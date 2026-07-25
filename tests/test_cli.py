from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiwatcher_cli import cli, local_state
from aiwatcher_cli.local_state import recent_decisions
from aiwatcher_cli.scanner import LocalEvent, LocalSession


def session(
    index: int,
    *,
    tool: str = "claude-code",
    age_days: int = 0,
    project: str = "/repo",
    notes: list[str] | None = None,
    now: datetime | None = None,
) -> LocalSession:
    # `now` lets callers pin every session in a batch to one shared instant.
    # Without it, back-to-back session(...) calls each take their own
    # datetime.now(), and the microsecond drift between the first and last
    # call can push a span like "exactly 18 days" to "17 days, 23:59:59.998"
    # -- a flaky day-boundary flip in any test asserting an exact
    # history_span_days from timedelta.days truncation.
    stamp = (now or datetime.now(timezone.utc)) - timedelta(days=age_days)
    return LocalSession(
        session_id=f"session-{index}",
        tool=tool,
        project_path=project,
        started_at=stamp - timedelta(minutes=15),
        updated_at=stamp,
        model="claude-sonnet",
        tokens_in=10_000 + index,
        tokens_out=5_000 + index,
        cost_usd=0.25,
        agent_calls=20,
        tool_calls=10,
        notes=notes or [],
    )


def _baselines_from_sessions(rows: list[LocalSession]) -> dict[str, object]:
    # estimate_prompt_savings() now reads cached baselines (get_baselines())
    # instead of scanning history live -- see P0-3. Route the test's synthetic
    # session rows through the real _compute_baselines() aggregation so these
    # tests still exercise the actual p75/honesty-gate math, not a hand-rolled
    # stand-in for it.
    with patch.object(cli, "sessions_since", return_value=rows):
        return cli._compute_baselines()


class PromptSavingsBaselineTests(unittest.TestCase):
    """P0-3: prompt gate must read cached baselines, never rescan history live."""

    def test_warm_cache_does_not_call_scanner(self) -> None:
        fresh = {
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "history_days": 30,
            "per_tool": {"claude-code": {
                "p75_tokens": 100_000, "p75_calls": 50, "p75_tool_calls": 20,
                "p75_api_value": 1.0, "session_count": 12, "history_span_days": 20,
                "excluded_cumulative": 0,
            }},
        }
        with (
            patch.object(cli, "get_baselines", return_value=fresh),
            patch.object(cli, "sessions_since") as sessions_since,
            patch.object(cli, "save_baselines") as save,
        ):
            result = cli.get_or_refresh_baselines()

        sessions_since.assert_not_called()
        save.assert_not_called()
        self.assertEqual(result, fresh)

    def test_stale_cache_is_recomputed_and_saved(self) -> None:
        stale = {
            "computed_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
            "history_days": 30,
            "per_tool": {},
        }
        with (
            patch.object(cli, "get_baselines", return_value=stale),
            patch.object(cli, "sessions_since", return_value=[]) as sessions_since,
            patch.object(cli, "save_baselines") as save,
        ):
            result = cli.get_or_refresh_baselines(max_age_hours=24)

        sessions_since.assert_called()
        save.assert_called_once()
        self.assertNotEqual(result["computed_at"], stale["computed_at"])

    def test_cold_hook_path_never_scans_history(self) -> None:
        # No cache at all (e.g. a brand new install) must still return
        # quickly with an insufficient-confidence result -- never fall back
        # to scanning, which is exactly the hot-path cost this fix removes.
        with (
            patch.object(cli, "get_baselines", return_value={}),
            patch.object(cli, "sessions_since") as sessions_since,
        ):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")

        sessions_since.assert_not_called()
        self.assertFalse(result["estimated_impact"]["available"])

    def test_unreadable_baseline_cache_does_not_break_preflight(self) -> None:
        # A hook/preflight path must fail soft if the local state file or
        # baseline lock is unavailable. It should never crash the user's AI
        # tool or fall back to a live history scan.
        with (
            patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")),
            patch.object(cli, "sessions_since") as sessions_since,
        ):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")

        sessions_since.assert_not_called()
        impact = result["estimated_impact"]
        self.assertFalse(impact["available"])
        self.assertEqual(impact["basis"], "no comparable local history")

    def test_today_continues_when_baseline_cache_is_unreadable(self) -> None:
        with (
            patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")),
            patch.object(cli, "scan_all", return_value=[]),
            patch.object(cli, "link_recent_interventions_to_sessions"),
            patch.object(cli, "_compute_baselines", return_value={
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "history_days": 30,
                "per_tool": {},
            }),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = cli.command_today(SimpleNamespace())

        self.assertEqual(result, 0)

    def test_report_continues_when_baseline_cache_is_unreadable(self) -> None:
        with (
            patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "_compute_baselines", return_value={
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "history_days": 30,
                "per_tool": {},
            }),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = cli.command_report(SimpleNamespace(days=7))

        self.assertEqual(result, 0)

    def test_ui_startup_continues_when_baseline_cache_is_unreadable(self) -> None:
        with (
            patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")),
            patch.object(cli, "_compute_baselines", return_value={
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "history_days": 30,
                "per_tool": {},
            }),
            patch("aiwatcher_cli.ui.serve") as serve,
        ):
            result = cli.command_ui(SimpleNamespace(
                host="127.0.0.1",
                port=8765,
                no_port_fallback=True,
                port_attempts=1,
                restart=False,
            ))

        self.assertEqual(result, 0)
        serve.assert_called_once()

    def test_today_refreshes_a_missing_cache(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        with (
            patch.object(cli, "scan_all", return_value=[]),
            patch.object(cli, "link_recent_interventions_to_sessions"),
            patch.object(cli, "get_baselines", return_value={}),
            patch.object(cli, "sessions_since", return_value=rows),
            patch.object(cli, "save_baselines") as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            cli.command_today(SimpleNamespace())

        save.assert_called_once()
        saved = save.call_args.args[0]
        self.assertEqual(saved["per_tool"]["claude-code"]["session_count"], 10)

    def test_fixed_fixture_matches_locked_estimate(self) -> None:
        # Regression lock: a known, fixed set of sessions must always produce
        # this exact estimate. Guards against the cache-based rewrite quietly
        # drifting from the pre-P0-3 live-scan math. All rows share one `now`
        # so history_span_days is an exact 18-day span, not sensitive to
        # inter-call timing drift (see session()'s `now` param docstring).
        now = datetime.now(timezone.utc)
        rows = [session(index, age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        self.assertEqual(baselines["per_tool"]["claude-code"], {
            "p75_tokens": 15014.0,
            "p75_calls": 20.0,
            "p75_tool_calls": 10.0,
            "p75_api_value": 0.25,
            "p75_tokens_per_minute": 15014.0 / 15.0,
            "session_count": 10,
            "history_span_days": 19,
            "excluded_cumulative": 0,
        })
        with patch.object(cli, "get_baselines", return_value=baselines):
            impact = cli.estimate_prompt_savings(
                "Refactor the entire codebase", risk_score=3, tool="claude", cwd="/repo"
            )
        self.assertEqual(impact, {
            "available": True,
            "confidence": "medium",
            "basis": "10 local claude sessions spanning 19 days",
            "sample_count": 10,
            "history_span_days": 19,
            "original": {
                "tokens": [25139, 52212],
                "model_calls": [33, 69],
                "tool_calls": [16, 34],
                "api_value_usd": [0.4186, 0.8694],
            },
            "safer": {
                "tokens": [11312, 32893],
                "model_calls": [14, 43],
                "tool_calls": [7, 21],
                "api_value_usd": [0.1884, 0.5477],
            },
            "savings": {
                "tokens": [13827, 19319],
                "model_calls": [19, 26],
                "tool_calls": [9, 13],
                "api_value_usd": [0.2302, 0.3217],
            },
        })


class PromptPreflightTests(unittest.TestCase):
    def test_sparse_history_does_not_show_quantified_savings(self) -> None:
        baselines = _baselines_from_sessions([session(1), session(2, age_days=2)])
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "Refactor the entire codebase and delete old auth secrets",
                tool="claude",
                cwd="/repo",
            )

        impact = result["estimated_impact"]
        self.assertFalse(impact["available"])
        rendered = cli.render_preflight(result)
        self.assertIn("Quantified savings unavailable", rendered)
        self.assertNotIn("Estimated savings:", rendered)

    def test_quantified_ranges_require_enough_history_over_time(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "Refactor the entire codebase, but inspect first",
                tool="claude",
                cwd="/repo",
            )

        impact = result["estimated_impact"]
        self.assertTrue(impact["available"])
        self.assertGreaterEqual(impact["sample_count"], cli.MIN_SAVINGS_SESSIONS)
        self.assertGreaterEqual(impact["history_span_days"], cli.MIN_SAVINGS_HISTORY_DAYS)
        self.assertIn("planning ranges", cli.render_preflight(result).lower())

    def test_codex_cumulative_totals_are_not_used_for_savings(self) -> None:
        rows = [
            session(
                index,
                tool="codex-cli",
                age_days=index * 2,
                notes=["tokens_used is Codex's cumulative thread total"],
            )
            for index in range(10)
        ]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "Refactor the entire codebase",
                tool="codex",
                cwd="/repo",
            )

        impact = result["estimated_impact"]
        self.assertFalse(impact["available"])
        self.assertIn("cumulative", impact["basis"])

    def test_codex_rollout_measurements_can_support_savings_ranges(self) -> None:
        rows = [session(index, tool="codex-cli", age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "Refactor the entire codebase",
                tool="codex",
                cwd="/repo",
            )

        self.assertTrue(result["estimated_impact"]["available"])

    def test_sessions_search_filters_project_tool_model_or_id(self) -> None:
        rows = [
            session(1, project="/repo/orcha"),
            session(2, tool="codex-cli", project="/repo/agentwatch"),
        ]
        args = SimpleNamespace(days=7, limit=20, team=False, search="orcha")
        output = io.StringIO()

        with patch.object(cli, "sessions_since", return_value=rows), patch("sys.stdout", output):
            result = cli.command_sessions(args)

        self.assertEqual(result, 0)
        self.assertIn("/repo/orcha", output.getvalue())
        self.assertNotIn("/repo/agentwatch", output.getvalue())

    def test_resume_uses_most_recent_matching_session(self) -> None:
        rows = [
            session(1, project="/repo/agentwatch"),
            session(2, project="/repo/orcha"),
        ]
        args = SimpleNamespace(
            session_id=None,
            search="orcha",
            days=7,
            target="codex",
            copy=False,
            format="text",
            include_prompt_excerpt=False,
        )

        with patch.object(cli, "sessions_since", return_value=rows), patch.object(cli, "command_handoff", return_value=0) as handoff:
            result = cli.command_resume(args)

        self.assertEqual(result, 0)
        self.assertEqual(args.session_id, "session-2")
        handoff.assert_called_once_with(args)

    def test_log_decision_defaults_to_latest_session(self) -> None:
        rows = [session(1, project="/repo/older", age_days=1), session(2, project="/repo/newest", age_days=0)]
        args = SimpleNamespace(
            session_id=None,
            days=7,
            summary="Considered a token-based tiebreaker",
            reasoning="Still picks turn #1 for unrelated reasons.",
            alternatives_rejected=["token-based tiebreaker", "git diff --stat only"],
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(cli, "sessions_since", return_value=rows),
                patch("sys.stdout", output),
            ):
                result = cli.command_log_decision(args)
                stored = recent_decisions("session-2")

        self.assertEqual(result, 0)
        self.assertIn("session-2", output.getvalue())
        self.assertIn("not verified against what actually happened", output.getvalue())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["summary"], "Considered a token-based tiebreaker")
        self.assertEqual(stored[0]["alternatives_rejected"], ["token-based tiebreaker", "git diff --stat only"])

    def test_log_decision_respects_explicit_session_id(self) -> None:
        args = SimpleNamespace(
            session_id="explicit-session",
            days=7,
            summary="Chose real commit subject/body over hashing",
            reasoning=None,
            alternatives_rejected=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                result = cli.command_log_decision(args)
                stored = recent_decisions("explicit-session")

        self.assertEqual(result, 0)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["session_id"], "explicit-session")

    def test_log_decision_rejects_empty_summary(self) -> None:
        args = SimpleNamespace(
            session_id="explicit-session",
            days=7,
            summary="   ",
            reasoning=None,
            alternatives_rejected=[],
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}), patch("sys.stderr", output):
                result = cli.command_log_decision(args)
                stored = recent_decisions("explicit-session")

        self.assertEqual(result, 2)
        self.assertEqual(stored, [])

    def test_install_decision_log_dry_run_writes_nothing(self) -> None:
        args = SimpleNamespace(write=False)
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            claude_md = os.path.join(temp_dir, ".claude", "CLAUDE.md")
            with (
                patch.dict(os.environ, {"HOME": temp_dir, "USERPROFILE": temp_dir}),
                patch("sys.stdout", output),
            ):
                result = cli.command_install_claude_decision_log(args)

        self.assertEqual(result, 0)
        self.assertFalse(os.path.exists(claude_md))
        self.assertIn("aiwatcher log-decision", output.getvalue())
        self.assertIn(claude_md.replace("\\", "/"), output.getvalue().replace("\\", "/"))

    def test_install_decision_log_writes_and_is_idempotent(self) -> None:
        args = SimpleNamespace(write=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            claude_md = os.path.join(temp_dir, ".claude", "CLAUDE.md")
            with patch.dict(os.environ, {"HOME": temp_dir, "USERPROFILE": temp_dir}):
                first = cli.command_install_claude_decision_log(args)
                with open(claude_md, encoding="utf-8") as handle:
                    first_content = handle.read()
                second = cli.command_install_claude_decision_log(args)
                with open(claude_md, encoding="utf-8") as handle:
                    second_content = handle.read()

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIn("aiwatcher log-decision", first_content)
        self.assertEqual(first_content, second_content)
        self.assertEqual(first_content.count(cli.DECISION_LOG_MARKER_START), 1)

    def test_install_decision_log_preserves_existing_content(self) -> None:
        args = SimpleNamespace(write=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            claude_dir = os.path.join(temp_dir, ".claude")
            os.makedirs(claude_dir)
            claude_md = os.path.join(claude_dir, "CLAUDE.md")
            with open(claude_md, "w", encoding="utf-8") as handle:
                handle.write("# My personal preferences\n\nAlways use tabs.\n")

            with patch.dict(os.environ, {"HOME": temp_dir, "USERPROFILE": temp_dir}):
                cli.command_install_claude_decision_log(args)
                with open(claude_md, encoding="utf-8") as handle:
                    content = handle.read()

        self.assertIn("Always use tabs.", content)
        self.assertIn("aiwatcher log-decision", content)

    def test_uninstall_decision_log_removes_block_and_preserves_rest(self) -> None:
        install_args = SimpleNamespace(write=True)
        uninstall_args = SimpleNamespace()

        with tempfile.TemporaryDirectory() as temp_dir:
            claude_dir = os.path.join(temp_dir, ".claude")
            os.makedirs(claude_dir)
            claude_md = os.path.join(claude_dir, "CLAUDE.md")
            with open(claude_md, "w", encoding="utf-8") as handle:
                handle.write("# My personal preferences\n\nAlways use tabs.\n")

            with patch.dict(os.environ, {"HOME": temp_dir, "USERPROFILE": temp_dir}):
                cli.command_install_claude_decision_log(install_args)
                result = cli.command_uninstall_claude_decision_log(uninstall_args)
                with open(claude_md, encoding="utf-8") as handle:
                    content = handle.read()

        self.assertEqual(result, 0)
        self.assertIn("Always use tabs.", content)
        self.assertNotIn("aiwatcher log-decision", content)
        self.assertNotIn(cli.DECISION_LOG_MARKER_START, content)

    def test_uninstall_decision_log_when_nothing_installed(self) -> None:
        args = SimpleNamespace()
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"HOME": temp_dir, "USERPROFILE": temp_dir}),
                patch("sys.stdout", output),
            ):
                result = cli.command_uninstall_claude_decision_log(args)

        self.assertEqual(result, 0)
        self.assertIn("No personal Claude memory file found", output.getvalue())

    def test_watch_points_high_pressure_session_to_resume(self) -> None:
        now = datetime.now(timezone.utc)
        latest = session(1, project="/repo/latest", now=now)
        row = session(2, project="/repo/orcha", now=now)
        row.updated_at = latest.updated_at - timedelta(minutes=1)
        row.started_at = row.updated_at - timedelta(minutes=15)
        row.agent_calls = 300
        args = SimpleNamespace(
            days=1,
            interval=15,
            once=True,
            cost_threshold=5.0,
            calls_threshold=250,
            tokens_threshold=500_000,
            target="generic",
        )
        output = io.StringIO()

        with (
            patch.object(cli, "sessions_since", return_value=[latest, row]),
            patch.object(cli, "scan_all_events", return_value=[]),
            patch.object(cli, "get_baselines", return_value={}),
            patch("sys.stdout", output),
        ):
            result = cli.command_watch(args)

        self.assertEqual(result, 0)
        self.assertIn("Latest observed session", output.getvalue())
        self.assertIn("Other sessions with local signals:", output.getvalue())
        self.assertIn("aiwatcher resume --session-id session-2 --target codex --copy", output.getvalue())

    def test_watch_labels_itself_as_log_based_not_live(self) -> None:
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[session(1)]),
            patch.object(cli, "scan_all_events", return_value=[]),
            patch.object(cli, "get_baselines", return_value={}),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        self.assertIn("not a live hook into a running agent", output.getvalue())
        self.assertIn("local logs only", output.getvalue())
        self.assertIn("may copy a local handoff brief", output.getvalue())

    def test_watch_critical_context_prints_handoff_capsule_inline(self) -> None:
        row = session(1, project="/repo/orcha")
        events = [
            LocalEvent(
                event_id=f"evt-{i}",
                session_id=row.session_id,
                tool=row.tool,
                event_type="assistant",
                timestamp=row.started_at,
                tokens_in=210_000,
                tokens_out=500,
            )
            for i in range(3)
        ]
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[row]),
            patch.object(cli, "scan_all_events", return_value=events),
            patch.object(cli, "get_baselines", return_value={}),
            patch.object(cli, "get_outcome", side_effect=OSError("read-only state")),
            patch.object(cli, "_copy_to_clipboard", return_value=(False, "no clipboard command found")),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        rendered = output.getvalue()
        self.assertIn("Context    : critical", rendered)
        self.assertIn("Recommended: create handoff capsule now", rendered)
        self.assertIn("generating a handoff capsule now", rendered)
        self.assertIn("AIWatcher handoff capsule", rendered)
        self.assertIn("Paste this as the first prompt in a fresh AI coding session.", rendered)

    def test_watch_critical_context_copies_to_clipboard_and_dedupes_across_polls(self) -> None:
        row = session(1, project="/repo/orcha")
        events = [
            LocalEvent(
                event_id=f"evt-{i}",
                session_id=row.session_id,
                tool=row.tool,
                event_type="assistant",
                timestamp=row.started_at,
                tokens_in=210_000,
                tokens_out=500,
            )
            for i in range(3)
        ]
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="codex")
        output = io.StringIO()
        critical_capsule_seen: dict = {}

        with (
            patch.object(cli, "sessions_since", return_value=[row]),
            patch.object(cli, "scan_all_events", return_value=events),
            patch.object(cli, "get_baselines", return_value={}),
            patch.object(cli, "get_outcome", return_value=None),
            patch.object(cli, "_copy_to_clipboard", return_value=(True, "clip")) as copy_mock,
        ):
            all_events = cli.events_by_session([row], days=1)
            with patch("sys.stdout", output):
                cli._print_watch_status_card(row, [row], args, all_events.get(row.session_id, []), critical_capsule_seen)
            copy_mock.assert_called_once()
            self.assertIn("Copied", output.getvalue())
            self.assertIn(row.session_id, critical_capsule_seen)

            # Second poll for the same (unchanged) session should not regenerate/recopy.
            second_output = io.StringIO()
            with patch("sys.stdout", second_output):
                cli._print_watch_status_card(row, [row], args, all_events.get(row.session_id, []), critical_capsule_seen)
            copy_mock.assert_called_once()  # still just the one call from the first poll
            self.assertIn("Capsule already generated", second_output.getvalue())
            self.assertIn(f"--target {args.target}", second_output.getvalue())

    def test_watch_healthy_session_recommends_continue(self) -> None:
        row = session(1)
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[row]),
            patch.object(cli, "scan_all_events", return_value=[]),
            patch.object(cli, "get_baselines", return_value={}),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        self.assertIn("Recommended: continue", output.getvalue())

    def test_watch_no_sessions_says_so_and_returns(self) -> None:
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with patch.object(cli, "sessions_since", return_value=[]), patch("sys.stdout", output):
            result = cli.command_watch(args)
        self.assertEqual(result, 0)
        self.assertIn("No local AI sessions detected", output.getvalue())

    def test_watch_runway_pressure_names_target_and_resume_command(self) -> None:
        now = datetime.now(timezone.utc)
        baseline_rows = [session(index, tool="claude-code", age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(baseline_rows)
        pressured = session(100, tool="claude-code", age_days=0, now=now)
        pressured.tokens_in = int(baselines["per_tool"]["claude-code"]["p75_tokens"] * 3)
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[pressured]),
            patch.object(cli, "scan_all_events", return_value=[]),
            patch.object(cli, "get_baselines", return_value=baselines),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        rendered = output.getvalue()
        self.assertIn("Recommended: switch tool or lane", rendered)
        self.assertIn(f"aiwatcher resume --session-id {pressured.session_id} --target codex --copy", rendered)
        self.assertIn("continue in Codex", rendered)

    def test_watch_other_sessions_surface_context_health_not_just_thresholds(self) -> None:
        now = datetime.now(timezone.utc)
        latest = session(1, project="/repo/latest", now=now)
        quiet_but_unhealthy = session(2, project="/repo/quiet", now=now)
        quiet_but_unhealthy.updated_at = latest.updated_at - timedelta(minutes=1)
        quiet_but_unhealthy.started_at = quiet_but_unhealthy.updated_at - timedelta(minutes=15)
        events = [
            LocalEvent(
                event_id=f"evt-{i}",
                session_id=quiet_but_unhealthy.session_id,
                tool=quiet_but_unhealthy.tool,
                event_type="assistant",
                timestamp=quiet_but_unhealthy.started_at,
                tokens_in=210_000,
                tokens_out=500,
            )
            for i in range(3)
        ]
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[latest, quiet_but_unhealthy]),
            patch.object(cli, "scan_all_events", return_value=events),
            patch.object(cli, "get_baselines", return_value={}),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        rendered = output.getvalue()
        self.assertIn("Other sessions with local signals:", rendered)
        self.assertIn("/repo/quiet", rendered)
        self.assertNotIn("/repo/latest |", rendered)
        self.assertIn("Context critical:", rendered)


class RunwayPressureTests(unittest.TestCase):
    def test_flags_high_window_usage_against_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [session(index, tool="claude-code", age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        window_rows = [session(90, tool="claude-code", age_days=0, now=now)]
        window_rows[0].tokens_in = 400_000
        window_rows[0].tokens_out = 100_000
        with patch.object(cli, "get_baselines", return_value=baselines):
            runway = cli._runway_pressure("claude-code", window_rows)
        self.assertIsNotNone(runway)
        self.assertGreaterEqual(runway["pressure_ratio"], 1.5)

    def test_returns_none_without_enough_baseline_history(self) -> None:
        baselines = _baselines_from_sessions([session(1, tool="claude-code")])
        with patch.object(cli, "get_baselines", return_value=baselines):
            runway = cli._runway_pressure("claude-code", [session(1, tool="claude-code")])
        self.assertIsNone(runway)

    def test_returns_none_outside_the_window(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [session(index, tool="claude-code", age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        old_row = session(90, tool="claude-code", age_days=1, now=now)
        with patch.object(cli, "get_baselines", return_value=baselines):
            runway = cli._runway_pressure("claude-code", [old_row])
        self.assertIsNone(runway)

    def test_returns_none_for_tool_without_baseline_support(self) -> None:
        runway = cli._runway_pressure("cursor", [session(1, tool="cursor")])
        self.assertIsNone(runway)


def _event(session_id, *, content_hash="h1", tokens_in=1000, tokens_out=200, timestamp=None, event_type="assistant_tool_use"):
    return LocalEvent(
        event_id=f"evt-{content_hash}-{timestamp}",
        session_id=session_id,
        tool="claude-code",
        event_type=event_type,
        timestamp=timestamp,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        content_hash=content_hash,
    )


class LoopSignalTests(unittest.TestCase):
    def test_flags_repeated_identical_content_at_the_threshold(self) -> None:
        events = [_event("s1", content_hash="dup") for _ in range(cli.LOOP_FLAG_REPEAT)]
        loop = cli._loop_signal(events)
        self.assertIsNotNone(loop)
        self.assertEqual(loop["max_repeat"], cli.LOOP_FLAG_REPEAT)
        self.assertIn("Possible loop", loop["diagnosis"])

    def test_none_below_the_threshold(self) -> None:
        events = [_event("s1", content_hash="dup") for _ in range(cli.LOOP_FLAG_REPEAT - 1)]
        self.assertIsNone(cli._loop_signal(events))

    def test_none_with_no_events(self) -> None:
        self.assertIsNone(cli._loop_signal([]))

    def test_distinct_content_is_not_a_loop(self) -> None:
        events = [_event("s1", content_hash=f"unique-{i}") for i in range(5)]
        self.assertIsNone(cli._loop_signal(events))


class VelocitySignalTests(unittest.TestCase):
    def test_flags_fast_recent_activity_against_real_duration_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [session(index, tool="claude-code", age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        # Baseline sessions run 15 real minutes each (see session()); burn the
        # same order of tokens in under a minute here to force a clear multiple.
        events = [
            _event("s1", content_hash=f"c{i}", tokens_in=5000, tokens_out=1000, timestamp=now - timedelta(seconds=i * 5))
            for i in range(6)
        ]
        with patch.object(cli, "get_baselines", return_value=baselines):
            velocity = cli._velocity_signal("claude-code", events)
        self.assertIsNotNone(velocity)
        self.assertGreaterEqual(velocity["ratio"], cli.VELOCITY_RATIO)

    def test_none_without_enough_baseline_history(self) -> None:
        baselines = _baselines_from_sessions([session(1, tool="claude-code")])
        events = [_event("s1", tokens_in=50000, timestamp=datetime.now(timezone.utc))]
        with patch.object(cli, "get_baselines", return_value=baselines):
            self.assertIsNone(cli._velocity_signal("claude-code", events))

    def test_none_for_tool_without_baseline_support(self) -> None:
        events = [_event("s1", tokens_in=50000, timestamp=datetime.now(timezone.utc))]
        self.assertIsNone(cli._velocity_signal("cursor", events))

    def test_none_with_too_few_recent_events(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [session(index, tool="claude-code", age_days=index * 2, now=now) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            self.assertIsNone(cli._velocity_signal("claude-code", [_event("s1", timestamp=now)]))


class WatchLoopAndVelocityIntegrationTests(unittest.TestCase):
    def test_watch_loop_seeded_capsule_leads_with_loop_diagnosis(self) -> None:
        row = session(1, project="/repo/orcha")
        events = [_event(row.session_id, content_hash="dup", timestamp=row.started_at) for _ in range(cli.LOOP_CAPSULE_REPEAT)]
        args = SimpleNamespace(days=1, interval=15, once=True, cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000, target="generic")
        output = io.StringIO()
        with (
            patch.object(cli, "sessions_since", return_value=[row]),
            patch.object(cli, "scan_all_events", return_value=events),
            patch.object(cli, "get_baselines", return_value={}),
            patch("sys.stdout", output),
        ):
            cli.command_watch(args)
        rendered = output.getvalue()
        self.assertIn("Recommended: create handoff capsule now", rendered)
        self.assertIn("Loop       : Possible loop", rendered)
        self.assertIn("AIWatcher handoff capsule", rendered)
        # The loop diagnosis must lead "Why hand off now", ahead of the generic checks.
        why_hand_off = rendered.index("Why hand off now")
        loop_in_capsule = rendered.index("Possible loop", why_hand_off)
        # The loop diagnosis is the extra_warnings entry -- it must come
        # first in the bullet list, ahead of the generic cost/call checks.
        first_bullet = rendered.index("- ", why_hand_off)
        self.assertEqual(loop_in_capsule, first_bullet + 2)

    def test_low_risk_prompt_does_not_render_impact_section(self) -> None:
        with patch.object(
            cli,
            "sessions_since",
            side_effect=AssertionError("low-risk prompts should not scan session history"),
        ):
            result = cli.analyze_prompt("Explain this function", tool="claude", cwd="/repo")

        rendered = cli.render_preflight(result)
        self.assertEqual(result["risk"], "low")
        self.assertNotIn("Expected impact", rendered)

    def test_scoped_execution_brief_reduces_risk_score(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            original = cli.analyze_prompt(
                "Refactor the entire codebase and delete old auth secrets",
                tool="claude",
                cwd="/repo",
            )
            selected = cli.analyze_prompt(
                str(original["suggested_prompt"]),
                tool="claude",
                cwd="/repo",
            )

        self.assertEqual(original["score"], 8)
        self.assertLess(selected["score"], original["score"])
        self.assertEqual(selected["risk"], "low")

    def test_guardrail_chips_match_triggered_findings(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(
                "refactor everything and delete the production credential",
                tool="claude",
                cwd="/repo",
            )

        labels = [g["label"] for g in result["guardrails"]]
        self.assertIn("Scope narrowed", labels)
        self.assertIn("Plan-first checkpoint", labels)
        self.assertIn("Confirm before destructive changes", labels)
        # One chip per triggered finding, not a copy of the full prose findings list.
        self.assertEqual(len(result["guardrails"]), len(result["findings"]))

    def test_broad_multi_file_ui_work_is_flagged_without_security_terms(self) -> None:
        # S-04: "Add a dark mode toggle to every page in the app" has no
        # auth/delete/secret keyword, so only a breadth heuristic (quantifier
        # + surface-area noun) can catch it.
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(
                "Add a dark mode toggle to every page in the app",
                tool="claude",
                cwd="/repo",
            )

        self.assertIn(result["risk"], ("medium", "high"))
        self.assertTrue(
            any("touches many files or pages" in finding for finding in result["findings"])
        )
        self.assertIn("Scope narrowed", [g["label"] for g in result["guardrails"]])

    def test_breadth_heuristic_catches_all_and_each_quantifiers_too(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            all_result = cli.analyze_prompt("Update the styling on all screens", tool="claude", cwd="/repo")
            each_result = cli.analyze_prompt("Rename this prop on each component", tool="claude", cwd="/repo")
            throughout_result = cli.analyze_prompt("Apply the new spacing throughout the app", tool="claude", cwd="/repo")

        for result in (all_result, each_result, throughout_result):
            self.assertIn(result["risk"], ("medium", "high"))

    def test_narrow_single_file_request_stays_low_risk(self) -> None:
        # Sanity check the breadth heuristic doesn't fire on ordinary narrow asks.
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(
                "Fix the off-by-one error in parse_date inside utils.py",
                tool="claude",
                cwd="/repo",
            )
        self.assertEqual(result["risk"], "low")

    def test_security_weakening_prompt_is_flagged(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            jwt_result = cli.analyze_prompt(
                "Update JWT auth to remove signature check so login is faster",
                tool="claude",
                cwd="/repo",
            )
            loose_auth_result = cli.analyze_prompt(
                "Make auth less strict so tests pass",
                tool="claude",
                cwd="/repo",
            )
            tls_result = cli.analyze_prompt(
                "Disable TLS certificate verification in the client",
                tool="claude",
                cwd="/repo",
            )

        for result in (jwt_result, loose_auth_result, tls_result):
            self.assertIn(result["risk"], ("medium", "high"))
            self.assertTrue(
                any("weakens or removes a security control" in finding for finding in result["findings"])
            )

    def test_security_weakening_heuristic_avoids_docs_ui_and_test_cleanup(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            ui_result = cli.analyze_prompt(
                "Remove the validation error message from the signup form UI",
                tool="claude",
                cwd="/repo",
            )
            docs_result = cli.analyze_prompt(
                "Update the auth docs to remove an obsolete screenshot",
                tool="claude",
                cwd="/repo",
            )
            tests_result = cli.analyze_prompt(
                "Remove dead code from token parser tests",
                tool="claude",
                cwd="/repo",
            )

        for result in (ui_result, docs_result, tests_result):
            self.assertFalse(
                any("weakens or removes a security control" in finding for finding in result["findings"])
            )
            self.assertNotIn(result["risk"], ("medium", "high"))

    def test_sensitive_token_phrase_still_gets_guardrail(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(
                "Rotate the production access token",
                tool="claude",
                cwd="/repo",
            )

        self.assertIn(result["risk"], ("medium", "high"))
        self.assertTrue(
            any("sensitive data" in finding for finding in result["findings"])
        )

    def test_hero_savings_label_omitted_without_sufficient_history(self) -> None:
        # Also patch get_baselines(), not just sessions_since(): P0-3 made the
        # estimator read the cached baseline file instead of scanning live, so
        # a machine with a real (non-empty) local AIWatcher history would
        # otherwise leak its own cached baselines into this "no history" case.
        baselines = _baselines_from_sessions([])
        with patch.object(cli, "sessions_since", return_value=[]), patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        self.assertIsNone(cli._hero_savings_label(result))

    def test_hero_savings_label_shows_compact_dollar_range_when_available(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        label = cli._hero_savings_label(result)
        self.assertIsNotNone(label)
        self.assertIn("avoidable", label)
        self.assertTrue(label.startswith("~$"))

    def test_gate_html_shows_guardrail_chips_and_savings_badge_above_the_fold(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "refactor everything and delete the production credential",
                tool="claude",
                cwd="/repo",
            )
        page = cli._prompt_gate_html(tool="claude", cwd="/repo", prompt="original prompt text", result=result)

        self.assertIn('class="pill savings"', page)
        self.assertIn("avoidable", page)
        self.assertIn('class="guardrails"', page)
        self.assertIn("Scope narrowed", page)
        self.assertIn("Confirm before destructive changes", page)
        # The chip row must appear before the detailed findings/brief cards,
        # so the glanceable summary renders above the fold, not after it.
        self.assertLess(page.index('class="guardrails"'), page.index("What AIWatcher noticed"))

    def test_hero_pressure_label_omitted_without_sufficient_history(self) -> None:
        baselines = _baselines_from_sessions([])
        with patch.object(cli, "sessions_since", return_value=[]), patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        self.assertIsNone(cli._hero_pressure_label(result))

    def test_hero_pressure_label_shows_tokens_and_tool_calls_when_available(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        label = cli._hero_pressure_label(result)
        self.assertIsNotNone(label)
        self.assertIn("tokens", label)
        self.assertIn("tool calls avoided", label)

    def test_gate_html_shows_pressure_caption_next_to_savings_badge(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        baselines = _baselines_from_sessions(rows)
        with patch.object(cli, "get_baselines", return_value=baselines):
            result = cli.analyze_prompt(
                "refactor everything and delete the production credential",
                tool="claude",
                cwd="/repo",
            )
        page = cli._prompt_gate_html(tool="claude", cwd="/repo", prompt="original prompt text", result=result)

        self.assertIn('class="pressure-caption"', page)
        self.assertIn("tool calls avoided", page)
        # The compact caption must sit right after the savings badge (both in
        # the header), not down in the "What AIWatcher noticed" detail card --
        # that's still where the full sentence (_impact_summary) lives.
        self.assertLess(page.index('class="pressure-caption"'), page.index("What AIWatcher noticed"))
        self.assertGreater(page.index('class="pressure-caption"'), page.index('class="pill savings"'))
        self.assertIn("Estimated avoidable pressure:", page)

    def test_prompt_gate_html_script_is_valid_javascript(self) -> None:
        # A raw newline character accidentally embedded inside a JS single-quoted
        # string (from writing '\n\n' instead of '\\n\\n' in the Python f-string)
        # silently breaks the *entire* inline <script> block -- sendDecision()
        # never gets defined, so every gate button does nothing when clicked,
        # with no visible error anywhere. This actually happened (417f070).
        # Content-only assertions (assertIn on HTML fragments) can't catch this
        # class of bug; only actually parsing the script can.
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available to check JS syntax")
        result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        page = cli._prompt_gate_html(tool="claude", cwd="/repo", prompt="original prompt text", result=result)
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name
        try:
            completed = subprocess.run([node, "-c", script_path], capture_output=True, text=True)
        finally:
            os.unlink(script_path)
        self.assertEqual(completed.returncode, 0, f"Prompt Gate's inline JS has a syntax error:\n{completed.stderr}")

    def test_split_brief_for_display_is_lossless_on_reassembly(self) -> None:
        full = cli.build_execution_brief(
            "Refactor everything in the auth module",
            cwd="/repo/auth",
            broad_scope=True,
            needs_checkpoint=True,
            sensitive_or_destructive=True,
            vague_scope=False,
            multiple_tasks=False,
        )
        core, suffix = cli._split_brief_for_display(full)
        self.assertNotIn("Working directory", core)
        self.assertNotIn("Completion report", core)
        self.assertIn("Working directory\n/repo/auth", suffix)
        self.assertIn("Completion report", suffix)
        # The split must be reversible -- nothing in the static suffix may be
        # dropped from what actually gets sent when reassembled.
        self.assertEqual(core + "\n\n" + suffix, full)

    def test_split_brief_for_display_handles_missing_cwd(self) -> None:
        full = cli.build_execution_brief(
            "fix the bug",
            cwd=None,
            broad_scope=False,
            needs_checkpoint=True,
            sensitive_or_destructive=False,
            vague_scope=False,
            multiple_tasks=False,
        )
        core, suffix = cli._split_brief_for_display(full)
        self.assertNotIn("Working directory", suffix)
        self.assertIn("Completion report", suffix)
        self.assertEqual(core + "\n\n" + suffix, full)

    def test_gate_html_collapses_static_boilerplate_but_keeps_it_recoverable(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        page = cli._prompt_gate_html(tool="claude", cwd="/repo", prompt="original prompt text", result=result)

        # The visible textarea must not contain the static suffix directly...
        textarea_start = page.index('<textarea id="brief">') + len('<textarea id="brief">')
        textarea_end = page.index("</textarea>")
        textarea_content = page[textarea_start:textarea_end]
        self.assertNotIn("Working directory", textarea_content)
        self.assertNotIn("Completion report", textarea_content)
        # ...but it must still be present somewhere on the page (collapsed),
        # and the send handler must reattach it before submitting.
        self.assertIn('id="brief-suffix"', page)
        self.assertIn("Working directory", page)
        self.assertIn("Completion report", page)
        self.assertIn("suffixEl.textContent", page)
        # The old standalone "Working directory: ..." line is gone -- it's
        # redundant with the collapsed footer now.
        self.assertNotIn("<p class=\"privacy\">Working directory:", page)

    def test_split_core_for_diff_separates_task_from_added_bullets(self) -> None:
        full = cli.build_execution_brief(
            "Refactor everything in the auth module",
            cwd="/repo/auth",
            broad_scope=True,
            needs_checkpoint=True,
            sensitive_or_destructive=True,
            vague_scope=False,
            multiple_tasks=False,
        )
        core, _ = cli._split_brief_for_display(full)
        task, bullets = cli._split_core_for_diff(core)

        self.assertEqual(task, "Refactor everything in the auth module")
        self.assertNotIn("Task", task)
        self.assertGreaterEqual(len(bullets), 3)
        self.assertTrue(any("phased plan" in b for b in bullets))
        self.assertTrue(any("Do not reveal secret values" in b for b in bullets))

    def test_gate_html_renders_task_and_added_bullets_with_distinct_styling(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(
                "refactor everything and delete the production credential",
                tool="claude",
                cwd="/repo",
            )
        page = cli._prompt_gate_html(
            tool="claude", cwd="/repo", prompt="refactor everything and delete the production credential", result=result
        )

        self.assertIn('class="brief-diff"', page)
        self.assertIn('class="brief-task"', page)
        self.assertIn('class="brief-added"', page)
        self.assertIn('class="added-line"', page)
        # The diff view renders before the raw editable textarea, so the
        # scannable version is what's seen first, not the wall of text.
        self.assertLess(page.index('class="brief-diff"'), page.index('id="brief"'))
        # The raw textarea is still present and unchanged -- editing/sending
        # must keep working exactly as before this purely visual change.
        self.assertIn('<details class="brief-edit">', page)
        self.assertIn('<textarea id="brief">', page)

    def test_pasting_a_generated_brief_back_in_does_not_double_wrap(self) -> None:
        """A brief AIWatcher already generated must not get a second Task/Execution
        approach/Completion report shell wrapped around it when resubmitted."""
        with patch.object(cli, "sessions_since", return_value=[]):
            first = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
            brief = str(first["suggested_prompt"])
            self.assertTrue(brief.startswith("Task\n"))

            second = cli.analyze_prompt(brief, tool="claude", cwd="/repo")

        self.assertEqual(second["risk"], "low")
        self.assertEqual(second["suggested_prompt"], "")
        self.assertEqual(brief.count("Execution approach"), 1)

    def test_interactive_preflight_can_forward_safer_prompt(self) -> None:
        result = {
            "risk": "high",
            "suggested_prompt": "Inspect first, then make the smallest safe change.",
        }
        with patch("builtins.input", return_value="u"):
            selected, decision = cli._choose_preflight_prompt(
                "Refactor everything",
                result,
                interactive=True,
            )
        self.assertEqual(selected, result["suggested_prompt"])
        self.assertEqual(decision, "suggested")

    def test_noninteractive_high_risk_prompt_is_blocked(self) -> None:
        result = {"risk": "high", "suggested_prompt": "Inspect first."}
        selected, decision = cli._choose_preflight_prompt(
            "Delete everything",
            result,
            interactive=False,
        )
        self.assertIsNone(selected)
        self.assertEqual(decision, "blocked")

    def test_agent_wrapper_forwards_selected_prompt_to_real_binary(self) -> None:
        args = SimpleNamespace(
            text="Refactor the entire codebase",
            prompt=[],
            agent="claude",
            cwd="/repo",
            apply_suggestion=True,
            yes=False,
            dry_run=False,
            binary="/opt/claude",
        )
        with (
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
            patch.object(cli, "scan_all", return_value=[]),
            patch.object(cli, "record_intervention", return_value="intervention-1"),
        ):
            result = cli.command_agent_prompt(args)
        self.assertEqual(result, 0)
        forwarded = run.call_args.args[0]
        self.assertEqual(forwarded[0], "/opt/claude")
        self.assertTrue(forwarded[1].startswith("Task\nRefactor the entire codebase"))
        self.assertIn("smallest relevant subsystem", forwarded[1])

    def test_execution_brief_preserves_intent_and_adds_relevant_safety(self) -> None:
        original = "Rotate the payment API key and delete the old credential"
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt(original, tool="claude", cwd="/repo/payments")

        brief = result["suggested_prompt"]
        self.assertTrue(brief.startswith(f"Task\n{original}"))
        self.assertIn("Do not reveal secret values", brief)
        self.assertIn("/repo/payments", brief)
        self.assertNotIn("Original task:", brief)

    def test_cumulative_codex_totals_do_not_trigger_session_pressure_alerts(self) -> None:
        row = session(1, tool="codex-cli")
        row.tokens_in = 500_000_000
        row.agent_calls = 500
        row.notes = ["tokens_used is Codex's cumulative thread total"]

        insights = cli.session_insights(row)

        self.assertFalse(any("Large context" in insight for insight in insights))
        self.assertFalse(any("Many model calls" in insight for insight in insights))

    def test_state_write_failure_does_not_block_real_agent(self) -> None:
        args = SimpleNamespace(
            text="Refactor the entire codebase",
            prompt=[],
            agent="claude",
            cwd="/repo",
            apply_suggestion=True,
            yes=False,
            dry_run=False,
            binary="/opt/claude",
        )
        with (
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
            patch.object(cli, "scan_all", return_value=[]),
            patch.object(cli, "record_intervention", side_effect=PermissionError("read-only state")),
        ):
            result = cli.command_agent_prompt(args)
        self.assertEqual(result, 0)
        run.assert_called_once()


class HeadlessPromptGateTests(unittest.TestCase):
    """P0-5: the prompt gate must not hang waiting for a display that isn't there."""

    GATE_RESULT = {
        "risk": "high",
        "score": 8,
        "tool": "claude",
        "findings": [],
        "suggestions": [],
        "suggested_prompt": "safer scoped brief",
        "estimated_impact": {},
    }

    def test_display_available_true_on_windows_regardless_of_env(self) -> None:
        with (
            patch.object(cli.os, "name", "nt"),
            patch.dict(os.environ, {"SSH_TTY": "/dev/ttys001", "DISPLAY": ""}, clear=True),
        ):
            self.assertTrue(cli._display_available())

    def test_display_available_false_for_ssh_session_without_display(self) -> None:
        with (
            patch.object(cli.os, "name", "posix"),
            patch.object(cli.sys, "platform", "darwin"),
            patch.dict(os.environ, {"SSH_TTY": "/dev/ttys001", "DISPLAY": ""}, clear=True),
        ):
            self.assertFalse(cli._display_available())

    def test_display_available_false_for_linux_without_display_even_without_ssh(self) -> None:
        with (
            patch.object(cli.os, "name", "posix"),
            patch.object(cli.sys, "platform", "linux"),
            patch.dict(os.environ, {"DISPLAY": ""}, clear=True),
        ):
            self.assertFalse(cli._display_available())

    def test_display_available_true_on_macos_desktop_without_display_var(self) -> None:
        # macOS's native GUI doesn't use $DISPLAY at all -- only Linux and an
        # active SSH session should treat a missing DISPLAY as "no display".
        with (
            patch.object(cli.os, "name", "posix"),
            patch.object(cli.sys, "platform", "darwin"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(cli._display_available())

    def test_run_prompt_gate_still_uses_browser_when_display_available(self) -> None:
        with (
            patch.object(cli, "_display_available", return_value=True),
            patch.object(cli, "webbrowser") as webbrowser_mock,
        ):
            webbrowser_mock.open.return_value = True
            gate = cli.run_prompt_gate(
                tool="claude", cwd="/repo", prompt="original prompt",
                result=self.GATE_RESULT, timeout_seconds=1,
            )
        webbrowser_mock.open.assert_called_once()
        self.assertIsNone(gate)  # nobody answered within the short timeout

    def test_run_prompt_gate_shows_terminal_gate_when_no_display_and_tty(self) -> None:
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = True
        with (
            patch.object(cli, "_display_available", return_value=False),
            patch.object(cli.sys, "stdin", fake_stdin),
            patch("builtins.input", return_value="b"),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            gate = cli.run_prompt_gate(
                tool="claude", cwd="/repo", prompt="original prompt", result=self.GATE_RESULT,
            )
        self.assertEqual(gate, {"decision": "use_brief", "prompt": "safer scoped brief"})

    def test_terminal_gate_all_four_options_map_to_browser_decision_names(self) -> None:
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = True
        cases = [
            ("o", {"decision": "run_original", "prompt": "original prompt"}),
            ("c", {"decision": "cancel", "prompt": ""}),
        ]
        for choice, expected in cases:
            with (
                patch.object(cli, "_display_available", return_value=False),
                patch.object(cli.sys, "stdin", fake_stdin),
                patch("builtins.input", return_value=choice),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                gate = cli.run_prompt_gate(
                    tool="claude", cwd="/repo", prompt="original prompt", result=self.GATE_RESULT,
                )
            self.assertEqual(gate, expected)

    def test_run_prompt_gate_auto_blocks_instantly_when_headless_without_tty(self) -> None:
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = False
        with (
            patch.object(cli, "_display_available", return_value=False),
            patch.object(cli.sys, "stdin", fake_stdin),
            patch.dict(os.environ, {}, clear=True),
        ):
            started = time.monotonic()
            gate = cli.run_prompt_gate(
                tool="claude", cwd="/repo", prompt="original prompt", result=self.GATE_RESULT,
                timeout_seconds=180,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(gate, {"decision": "auto_block_headless", "prompt": ""})

    def test_run_prompt_gate_auto_briefs_instantly_when_configured(self) -> None:
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = False
        with (
            patch.object(cli, "_display_available", return_value=False),
            patch.object(cli.sys, "stdin", fake_stdin),
            patch.dict(os.environ, {"AIWATCHER_GATE_DEFAULT": "brief"}, clear=True),
        ):
            started = time.monotonic()
            gate = cli.run_prompt_gate(
                tool="claude", cwd="/repo", prompt="original prompt", result=self.GATE_RESULT,
                timeout_seconds=180,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(gate, {"decision": "auto_brief_headless", "prompt": "safer scoped brief"})

    def test_run_prompt_gate_falls_back_when_webbrowser_open_returns_false(self) -> None:
        # A display can look available yet webbrowser.open() still can't
        # launch anything (no known browser controller) -- must not wait out
        # the full timeout for a decision that will never come.
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = False
        with (
            patch.object(cli, "_display_available", return_value=True),
            patch.object(cli, "webbrowser") as webbrowser_mock,
            patch.object(cli.sys, "stdin", fake_stdin),
            patch.dict(os.environ, {}, clear=True),
        ):
            webbrowser_mock.open.return_value = False
            started = time.monotonic()
            gate = cli.run_prompt_gate(
                tool="claude", cwd="/repo", prompt="original prompt", result=self.GATE_RESULT,
                timeout_seconds=180,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(gate, {"decision": "auto_block_headless", "prompt": ""})

    def test_claude_hook_records_auto_block_headless_decision(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", return_value={"decision": "auto_block_headless", "prompt": ""}),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_claude_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["decision"], "block")
        self.assertEqual(record.call_args.kwargs["decision"], "auto_block_headless")

    def test_claude_hook_records_auto_brief_headless_decision(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(
                cli, "run_prompt_gate",
                return_value={"decision": "auto_brief_headless", "prompt": "Edited safe brief"},
            ),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_claude_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertIn("Edited safe brief", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(record.call_args.kwargs["decision"], "auto_brief_headless")

    def test_cursor_hook_records_auto_block_headless_decision(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", return_value={"decision": "auto_block_headless", "prompt": ""}),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_cursor_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["continue"])
        self.assertEqual(record.call_args.kwargs["decision"], "auto_block_headless")

    def test_cursor_hook_medium_risk_uses_gate_when_requested(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(
                cli,
                "run_prompt_gate",
                return_value={"decision": "use_brief", "prompt": "Scoped Cursor brief"},
            ) as gate_mock,
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_cursor_hook(args)

        self.assertEqual(result, 0)
        gate_mock.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["continue"])
        self.assertIn("Scoped Cursor brief", output["user_message"])
        self.assertEqual(record.call_args.kwargs["decision"], "brief_accepted")


class CommandGateAnalyzerTests(unittest.TestCase):
    def test_flags_rm_rf_combined_flag(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "rm -rf /tmp/build"})
        self.assertIsNotNone(match)
        self.assertEqual(match["pattern_id"], "rm-rf")

    def test_flags_rm_rf_separate_flags(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "rm -r -f node_modules"})
        self.assertEqual(match["pattern_id"], "rm-rf")

    def test_flags_rm_rf_long_flags(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "rm --recursive --force build"})
        self.assertEqual(match["pattern_id"], "rm-rf")

    def test_rm_force_only_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "rm -f singlefile.txt"}))

    def test_rm_without_flags_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "rm build/output.txt"}))

    def test_flags_git_push_force_long_flag(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "git push origin main --force"})
        self.assertEqual(match["pattern_id"], "git-push-force")

    def test_flags_git_push_force_short_flag(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "git push -f origin main"})
        self.assertEqual(match["pattern_id"], "git-push-force")

    def test_ordinary_git_push_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "git push origin main"}))

    def test_flags_git_reset_hard(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "git reset --hard HEAD~3"})
        self.assertEqual(match["pattern_id"], "git-reset-hard")

    def test_soft_git_reset_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "git reset HEAD~3"}))

    def test_flags_credential_file_read(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "cat .env"})
        self.assertEqual(match["pattern_id"], "credential-read")

    def test_ordinary_file_read_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "cat README.md"}))

    def test_flags_prod_connection_string(self) -> None:
        match = cli.analyze_tool_call("Bash", {"command": "psql postgres://user:pw@prod-db.example.com/app"})
        self.assertEqual(match["pattern_id"], "prod-connection-string")

    def test_local_connection_string_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": "psql postgres://user:pw@localhost/app"}))

    def test_non_bash_tools_are_never_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Write", {"command": "rm -rf /"}))
        self.assertIsNone(cli.analyze_tool_call("Read", {"file_path": ".env"}))

    def test_empty_or_missing_command_is_not_flagged(self) -> None:
        self.assertIsNone(cli.analyze_tool_call("Bash", {"command": ""}))
        self.assertIsNone(cli.analyze_tool_call("Bash", {}))


class CommandGateHookTests(unittest.TestCase):
    def test_safe_command_passes_through_with_empty_output(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "s1"})
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "run_command_gate") as gate_mock,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_claude_pretooluse_hook(args)
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")
        gate_mock.assert_not_called()

    def test_blocked_decision_denies_and_records_real_command_text(self) -> None:
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/build"},
            "session_id": "s1",
            "cwd": "/repo",
        })
        args = SimpleNamespace(text=None, gate=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.object(cli, "_read_stdin_text", return_value=payload),
                patch.object(cli, "run_command_gate", return_value="block"),
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                result = cli.command_claude_pretooluse_hook(args)
            self.assertEqual(result, 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("rm -rf /tmp/build", output["hookSpecificOutput"]["permissionDecisionReason"])

            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                decisions = cli.recent_command_decisions(limit=5)
            self.assertEqual(len(decisions), 1)
            # The scenario spec explicitly requires the full real command text,
            # unlike prompts (which are hashed) -- confirm it is NOT hashed.
            self.assertEqual(decisions[0]["command"], "rm -rf /tmp/build")
            self.assertEqual(decisions[0]["decision"], "block")
            self.assertEqual(decisions[0]["session_id"], "s1")

    def test_allow_once_permits_without_persisting_always_allow(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}, "session_id": "s1"})
        args = SimpleNamespace(text=None, gate=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.object(cli, "_read_stdin_text", return_value=payload),
                patch.object(cli, "run_command_gate", return_value="allow_once"),
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                result = cli.command_claude_pretooluse_hook(args)
            self.assertEqual(result, 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertFalse(cli.is_command_pattern_always_allowed("git-reset-hard"))

    def test_always_allow_persists_and_skips_the_gate_next_time(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}, "session_id": "s1"})
        args = SimpleNamespace(text=None, gate=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.object(cli, "_read_stdin_text", return_value=payload),
                patch.object(cli, "run_command_gate", return_value="always_allow") as gate_mock,
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                cli.command_claude_pretooluse_hook(args)
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertTrue(cli.is_command_pattern_always_allowed("git-reset-hard"))

            # Second occurrence of the same pattern must not re-open the gate.
            with (
                patch.object(cli, "_read_stdin_text", return_value=payload),
                patch.object(cli, "run_command_gate", return_value="block") as gate_mock_2,
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout2,
            ):
                result = cli.command_claude_pretooluse_hook(args)
            self.assertEqual(result, 0)
            output = json.loads(stdout2.getvalue())
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
            gate_mock_2.assert_not_called()

    def test_gate_timeout_fails_closed(self) -> None:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}, "session_id": "s1"})
        args = SimpleNamespace(text=None, gate=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.object(cli, "_read_stdin_text", return_value=payload),
                patch.object(cli, "run_command_gate", return_value=None),
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                result = cli.command_claude_pretooluse_hook(args)
            self.assertEqual(result, 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")


class IntegrationConfigTests(unittest.TestCase):
    def test_command_gate_hook_uses_pretoolusehook_transport_and_bash_matcher(self) -> None:
        merged = cli._merge_claude_command_gate_hook({}, "python -m aiwatcher_cli")
        entry = merged["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "Bash")
        hook = entry["hooks"][0]
        self.assertIn("claude-pretooluse-hook", hook["command"])
        self.assertEqual(hook["timeout"], cli.PROMPT_GATE_HOST_TIMEOUT_SECONDS)

    def test_command_gate_hook_does_not_clobber_existing_user_prompt_submit_hook(self) -> None:
        existing = cli._merge_claude_hook({}, "python -m aiwatcher_cli", gate=True)
        merged = cli._merge_claude_command_gate_hook(existing, "python -m aiwatcher_cli")
        self.assertIn("UserPromptSubmit", merged["hooks"])
        self.assertIn("PreToolUse", merged["hooks"])

    def test_remove_command_gate_hook_preserves_other_hooks(self) -> None:
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "aiwatcher claude-pretooluse-hook"}]},
                    {"matcher": "Write", "hooks": [{"type": "command", "command": "other-check"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "finish"}]}],
            }
        }
        updated, removed = cli._remove_claude_command_gate_hook(settings)
        self.assertTrue(removed)
        self.assertEqual(len(updated["hooks"]["PreToolUse"]), 1)
        self.assertIn("Stop", updated["hooks"])

    def test_remove_command_gate_hook_reinstall_is_idempotent(self) -> None:
        merged = cli._merge_claude_command_gate_hook({}, "python -m aiwatcher_cli")
        merged_again = cli._merge_claude_command_gate_hook(merged, "python -m aiwatcher_cli")
        self.assertEqual(len(merged_again["hooks"]["PreToolUse"]), 1)

    def test_remove_claude_hook_preserves_other_hooks(self) -> None:
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "aiwatcher claude-hook"}]},
                    {"hooks": [{"type": "command", "command": "other-check"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "finish"}]}],
            }
        }
        updated, removed = cli._remove_claude_hook(settings)
        self.assertTrue(removed)
        self.assertEqual(len(updated["hooks"]["UserPromptSubmit"]), 1)
        self.assertIn("Stop", updated["hooks"])

    def test_remove_codex_wrapper_preserves_shell_content(self) -> None:
        content = (
            "export KEEP=1\n"
            f"{cli.CODEX_WRAPPER_MARKER_START}\nfunction codex() {{ :; }}\n"
            f"{cli.CODEX_WRAPPER_MARKER_END}\n"
            "alias ll='ls -l'\n"
        )
        updated, removed = cli._remove_marked_block(
            content,
            cli.CODEX_WRAPPER_MARKER_START,
            cli.CODEX_WRAPPER_MARKER_END,
        )
        self.assertTrue(removed)
        self.assertIn("export KEEP=1", updated)
        self.assertIn("alias ll=", updated)
        self.assertNotIn("function codex", updated)

    def test_gated_claude_hook_raises_host_timeout_past_decision_window(self) -> None:
        # Claude kills a hook after its own default timeout (much shorter than
        # AIWatcher's decision window), discarding the gate decision even
        # though the page looked alive. --gate must raise the host's timeout
        # past PROMPT_GATE_TIMEOUT_SECONDS or every gated decision is lost.
        merged = cli._merge_claude_hook({}, "python -m aiwatcher_cli", gate=True)
        hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], cli.PROMPT_GATE_HOST_TIMEOUT_SECONDS)
        self.assertGreater(cli.PROMPT_GATE_HOST_TIMEOUT_SECONDS, cli.PROMPT_GATE_TIMEOUT_SECONDS)

    def test_non_gated_claude_hook_does_not_set_a_timeout(self) -> None:
        merged = cli._merge_claude_hook({}, "python -m aiwatcher_cli", gate=False)
        hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertNotIn("timeout", hook)

    def test_gated_codex_hook_raises_host_timeout_past_decision_window(self) -> None:
        merged = cli._merge_codex_hook({}, "python -m aiwatcher_cli", gate=True)
        hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], cli.PROMPT_GATE_HOST_TIMEOUT_SECONDS)

    def test_codex_hook_merge_and_remove_preserves_other_hooks(self) -> None:
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "other-check"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "finish"}]}],
            }
        }
        merged = cli._merge_codex_hook(settings, "python -m aiwatcher_cli")
        self.assertEqual(len(merged["hooks"]["UserPromptSubmit"]), 2)
        self.assertIn("codex-hook", json.dumps(merged))

        updated, removed = cli._remove_codex_hook(merged)
        self.assertTrue(removed)
        self.assertEqual(len(updated["hooks"]["UserPromptSubmit"]), 1)
        self.assertIn("Stop", updated["hooks"])

    def test_read_stdin_text_decodes_utf8_regardless_of_platform_default(self) -> None:
        payload = json.dumps({"prompt": "Scan macro signals — short–term and long–term effect"})
        stdin = SimpleNamespace(buffer=io.BytesIO(payload.encode("utf-8")))
        with patch.object(cli.sys, "stdin", stdin):
            text = cli._read_stdin_text()
        decoded_prompt = json.loads(text)["prompt"]
        self.assertEqual(decoded_prompt, json.loads(payload)["prompt"])
        self.assertIn("—", decoded_prompt)

    def test_codex_hook_adds_execution_brief_for_medium_risk(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_intervention"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertIn("execution brief", output["systemMessage"].lower())
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Task\nRefactor the entire codebase", context)

    def test_extract_session_meta_reads_top_level_keys(self) -> None:
        meta = cli._extract_session_meta({
            "session_id": "sess-abc123",
            "transcript_path": "/home/dev/.claude/projects/x/sess-abc123.jsonl",
        })
        self.assertEqual(meta["session_id"], "sess-abc123")
        self.assertEqual(meta["transcript_path"], "/home/dev/.claude/projects/x/sess-abc123.jsonl")

    def test_extract_session_meta_falls_back_to_nested_hook_event(self) -> None:
        meta = cli._extract_session_meta({"hook_event": {"session_id": "sess-nested"}})
        self.assertEqual(meta["session_id"], "sess-nested")
        self.assertIsNone(meta["transcript_path"])

    def test_extract_session_meta_ignores_non_string_and_missing_values(self) -> None:
        meta = cli._extract_session_meta({"session_id": 12345, "cwd": "/repo"})
        self.assertIsNone(meta["session_id"])
        self.assertIsNone(meta["transcript_path"])

    def test_claude_hook_stores_session_id_on_intervention(self) -> None:
        payload = json.dumps({
            "prompt": "Refactor the entire codebase",
            "cwd": "/repo",
            "session_id": "sess-real-id",
        })
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_intervention") as record,
            patch.object(cli, "record_hook_event"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = cli.command_claude_hook(args)

        self.assertEqual(result, 0)
        self.assertEqual(record.call_args.kwargs["session_id"], "sess-real-id")

    def test_claude_hook_without_session_id_still_records(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_intervention") as record,
            patch.object(cli, "record_hook_event") as hook_event,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = cli.command_claude_hook(args)

        self.assertEqual(result, 0)
        self.assertIsNone(record.call_args.kwargs["session_id"])
        self.assertIsNone(hook_event.call_args.kwargs["session_id"])

    def test_debug_hook_keys_env_var_logs_keys_not_values(self) -> None:
        payload = json.dumps({"prompt": "delete the secret file", "session_id": "sess-secret"})
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_intervention"),
            patch.object(cli, "record_hook_event"),
            patch.dict(os.environ, {"AIWATCHER_DEBUG_HOOK_KEYS": "1"}),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            cli.command_claude_hook(args)

        logged = stderr.getvalue()
        self.assertIn("prompt", logged)
        self.assertIn("session_id", logged)
        self.assertNotIn("delete the secret file", logged)
        self.assertNotIn("sess-secret", logged)

    def test_codex_hook_medium_risk_uses_gate_when_requested(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(
                cli,
                "run_prompt_gate",
                return_value={"decision": "edit", "prompt": "Edited medium-risk brief"},
            ) as gate_mock,
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        gate_mock.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertIn("Edited medium-risk brief", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(record.call_args.kwargs["decision"], "brief_edited")

    def test_codex_prompt_gate_can_allow_original_prompt(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", return_value={"decision": "run_original", "prompt": ""}),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertEqual(record.call_args.kwargs["decision"], "allowed_original")

    def test_codex_prompt_gate_can_use_edited_brief_for_high_risk(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", return_value={"decision": "edit", "prompt": "Edited safe brief"}),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertIn("Edited safe brief", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(record.call_args.kwargs["decision"], "brief_edited")

    def test_codex_prompt_gate_cancel_blocks_run(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", return_value={"decision": "cancel", "prompt": ""}),
            patch.object(cli, "record_intervention") as record,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["decision"], "block")
        self.assertEqual(record.call_args.kwargs["decision"], "cancelled")

    def test_codex_prompt_gate_failure_falls_back_to_policy(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase and delete old auth secrets", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate", side_effect=OSError("bind failed")),
            patch.object(cli, "record_intervention"),
            patch.object(cli, "record_hook_event") as hook_event,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["decision"], "block")
        self.assertTrue(any(call.kwargs["event"] == "gate_failed" for call in hook_event.call_args_list))

    def test_install_codex_hook_can_generate_prompt_gate_command(self) -> None:
        merged = cli._merge_codex_hook({}, "python -m aiwatcher_cli", gate=True)
        self.assertIn("codex-hook --gate", json.dumps(merged))
        hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertGreater(hook["timeout"], cli.PROMPT_GATE_TIMEOUT_SECONDS)

    def test_claude_prompt_gate_host_timeout_exceeds_browser_gate(self) -> None:
        merged = cli._merge_claude_hook({}, "python -m aiwatcher_cli", gate=True)
        hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertGreater(hook["timeout"], cli.PROMPT_GATE_TIMEOUT_SECONDS)
        self.assertIn("statusMessage", hook)

    def test_prompt_gate_http_decision_completes_before_server_shutdown(self) -> None:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("loopback sockets are unavailable in this test sandbox")
        finally:
            probe.close()
        urls: queue.Queue[str] = queue.Queue()
        gate_result: list[dict[str, str] | None] = []
        with patch.object(cli, "sessions_since", return_value=[]):
            analysis = cli.analyze_prompt(
                "Refactor the entire codebase and delete old auth secrets",
                tool="claude",
                cwd="/repo",
            )

        def run_gate() -> None:
            gate_result.append(cli.run_prompt_gate(
                tool="claude",
                cwd="/repo",
                prompt="Refactor the entire codebase and delete old auth secrets",
                result=analysis,
                timeout_seconds=5,
                open_browser=False,
                ready_callback=urls.put,
            ))

        worker = threading.Thread(target=run_gate)
        worker.start()
        url = urls.get(timeout=2)
        request = urllib.request.Request(
            url + "decision",
            data=json.dumps({
                "decision": "use_brief",
                "prompt": analysis["suggested_prompt"],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            saved = json.load(response)
        worker.join(timeout=12)

        self.assertFalse(worker.is_alive())
        self.assertEqual(saved["decision_label"], "Add safer brief")
        self.assertEqual(gate_result[0]["decision"], "use_brief")

    def test_cursor_hook_merge_preserves_existing_hooks(self) -> None:
        settings = {
            "version": 1,
            "hooks": {
                "beforeSubmitPrompt": [{"command": "python existing.py"}],
                "stop": [{"command": "python stop.py"}],
            },
        }
        merged = cli._merge_cursor_hook(settings, "python -m aiwatcher_cli", gate=True)
        self.assertEqual(len(merged["hooks"]["beforeSubmitPrompt"]), 2)
        self.assertIn("cursor-hook --gate", json.dumps(merged))
        self.assertIn("stop", merged["hooks"])

        updated, removed = cli._remove_cursor_hook(merged)
        self.assertTrue(removed)
        self.assertEqual(len(updated["hooks"]["beforeSubmitPrompt"]), 1)

    def test_cursor_hook_allows_low_risk_prompt(self) -> None:
        payload = json.dumps({"prompt": "Explain this function", "workspace_roots": ["/repo"]})
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_hook_event"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_cursor_hook(args)

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"continue": True})

    def test_cursor_hook_pauses_risky_prompt_with_resubmittable_brief(self) -> None:
        payload = json.dumps({
            "prompt": "Refactor the entire codebase and delete old auth secrets",
            "workspace_roots": ["/repo"],
        })
        args = SimpleNamespace(text=None, gate=False)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "record_hook_event"),
            patch.object(cli, "record_intervention"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_cursor_hook(args)

        self.assertEqual(result, 0)
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["continue"])
        self.assertIn("scoped execution brief", output["user_message"].lower())
        self.assertIn("Task\nRefactor the entire codebase", output["user_message"])

    def test_public_hook_command_uses_module_entrypoint(self) -> None:
        command = cli._cli_command_for_current_file()
        self.assertIn("-m aiwatcher_cli", command)
        self.assertNotIn("collector/cli.py", command)

    def test_windows_hook_command_uses_forward_slashes_for_bash(self) -> None:
        # Claude/Codex/Cursor run hook commands through Git Bash even on
        # Windows. An unquoted backslash path like C:\Users\... gets mangled
        # to C:Users... there, so the generated command must not contain any
        # backslashes regardless of what sys.executable reports.
        with (
            patch.object(cli.sys, "executable", r"C:\Users\tadan\Python\python.exe"),
            patch.object(cli.os, "name", "nt"),
        ):
            command = cli._cli_command_for_current_file()
        self.assertNotIn("\\", command)
        self.assertIn("C:/Users/tadan/Python/python.exe", command)

    def test_windows_hook_command_quotes_paths_with_spaces(self) -> None:
        with (
            patch.object(cli.sys, "executable", r"C:\Program Files\Python\python.exe"),
            patch.object(cli.os, "name", "nt"),
        ):
            command = cli._cli_command_for_current_file()
        self.assertIn("'C:/Program Files/Python/python.exe'", command)

    def test_internal_hook_transport_is_not_in_public_parser(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict) and action.choices
        )
        self.assertNotIn("codex-hook", subparsers.choices)
        self.assertNotIn("claude-hook", subparsers.choices)
        self.assertNotIn("cursor-hook", subparsers.choices)

    def test_hook_status_connects_invocation_to_preflight_decision(self) -> None:
        with (
            patch.object(cli, "recent_hook_events", return_value=[{
                "created_at": "2026-07-03T12:00:00+00:00",
                "tool": "claude",
                "event": "received",
                "prompt_found": True,
                "risk": "high",
                "score": 8,
            }]),
            patch.object(cli, "recent_interventions", return_value=[{
                "created_at": "2026-07-03T12:00:05+00:00",
                "tool": "claude",
                "decision": "brief_edited",
                "score": 8,
                "selected_score": 2,
            }]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_hook_status(SimpleNamespace())

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("prompt found | risk high | score 8", output)
        self.assertIn("action added edited brief", output)
        self.assertIn("brief_edited (added edited brief) | risk score 8 -> 2", output)

    def test_hook_status_explains_context_added_is_not_a_popup(self) -> None:
        with (
            patch.object(cli, "recent_hook_events", return_value=[{
                "created_at": "2026-07-03T12:00:00+00:00",
                "tool": "codex",
                "event": "received",
                "prompt_found": True,
                "risk": "medium",
                "score": 5,
                "cwd": "/repo",
            }]),
            patch.object(cli, "recent_interventions", return_value=[{
                "created_at": "2026-07-03T12:00:05+00:00",
                "tool": "codex",
                "cwd": "/repo",
                "decision": "context_added",
                "score": 5,
                "selected_score": 2,
            }]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_hook_status(SimpleNamespace())

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("action added brief context (no popup)", output)

    def test_hook_status_shows_session_id_when_present(self) -> None:
        with (
            patch.object(cli, "recent_hook_events", return_value=[{
                "created_at": "2026-07-03T12:00:00+00:00",
                "tool": "claude",
                "event": "received",
                "prompt_found": True,
                "risk": "high",
                "score": 8,
                "session_id": "sess-real-id",
            }]),
            patch.object(cli, "recent_interventions", return_value=[{
                "created_at": "2026-07-03T12:00:05+00:00",
                "tool": "claude",
                "decision": "brief_edited",
                "score": 8,
                "selected_score": 2,
                "session_id": "sess-real-id",
            }]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_hook_status(SimpleNamespace())

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("| session sess-real-id", output)
        self.assertEqual(output.count("sess-real-id"), 2)


if __name__ == "__main__":
    unittest.main()
