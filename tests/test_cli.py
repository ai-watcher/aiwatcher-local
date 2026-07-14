from __future__ import annotations

import io
import json
import os
import queue
import socket
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli
from aiwatcher_cli.local_state import recent_decisions
from aiwatcher_cli.scanner import LocalSession


def session(
    index: int,
    *,
    tool: str = "claude-code",
    age_days: int = 0,
    project: str = "/repo",
    notes: list[str] | None = None,
) -> LocalSession:
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
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


class PromptPreflightTests(unittest.TestCase):
    def test_sparse_history_does_not_show_quantified_savings(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[session(1), session(2, age_days=2)]):
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
        with patch.object(cli, "sessions_since", return_value=rows):
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
        with patch.object(cli, "sessions_since", return_value=rows):
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
        with patch.object(cli, "sessions_since", return_value=rows):
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
        row = session(1, project="/repo/orcha")
        row.agent_calls = 300
        args = SimpleNamespace(
            days=1,
            interval=15,
            once=True,
            cost_threshold=5.0,
            calls_threshold=250,
            tokens_threshold=500_000,
        )
        output = io.StringIO()

        with patch.object(cli, "sessions_since", return_value=[row]), patch("sys.stdout", output):
            result = cli.command_watch(args)

        self.assertEqual(result, 0)
        self.assertIn("aiwatcher resume --session-id session-1 --target codex --copy", output.getvalue())

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

    def test_hero_savings_label_omitted_without_sufficient_history(self) -> None:
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        self.assertIsNone(cli._hero_savings_label(result))

    def test_hero_savings_label_shows_compact_dollar_range_when_available(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        with patch.object(cli, "sessions_since", return_value=rows):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        label = cli._hero_savings_label(result)
        self.assertIsNotNone(label)
        self.assertIn("avoidable", label)
        self.assertTrue(label.startswith("~$"))

    def test_gate_html_shows_guardrail_chips_and_savings_badge_above_the_fold(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        with patch.object(cli, "sessions_since", return_value=rows):
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
        with patch.object(cli, "sessions_since", return_value=[]):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        self.assertIsNone(cli._hero_pressure_label(result))

    def test_hero_pressure_label_shows_tokens_and_tool_calls_when_available(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        with patch.object(cli, "sessions_since", return_value=rows):
            result = cli.analyze_prompt("Refactor the entire codebase", tool="claude", cwd="/repo")
        label = cli._hero_pressure_label(result)
        self.assertIsNotNone(label)
        self.assertIn("tokens", label)
        self.assertIn("tool calls avoided", label)

    def test_gate_html_shows_pressure_caption_next_to_savings_badge(self) -> None:
        rows = [session(index, age_days=index * 2) for index in range(10)]
        with patch.object(cli, "sessions_since", return_value=rows):
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


class IntegrationConfigTests(unittest.TestCase):
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

    def test_codex_hook_medium_risk_skips_gate_even_when_requested(self) -> None:
        """Medium risk must never open the browser gate, even with gate=True —
        only high risk warrants the round-trip. Silent context injection instead."""
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
        args = SimpleNamespace(text=None, gate=True)
        with (
            patch.object(cli, "_read_stdin_text", return_value=payload),
            patch.object(cli, "sessions_since", return_value=[]),
            patch.object(cli, "run_prompt_gate") as gate_mock,
            patch.object(cli, "record_intervention"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = cli.command_codex_hook(args)

        self.assertEqual(result, 0)
        gate_mock.assert_not_called()
        output = json.loads(stdout.getvalue())
        self.assertIn("execution brief", output["systemMessage"].lower())

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
        self.assertIn("brief_edited | risk score 8 -> 2", output)


if __name__ == "__main__":
    unittest.main()
