from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli
from aiwatcher_cli.scanner import LocalSession


def session(
    index: int,
    *,
    tool: str = "claude-code",
    age_days: int = 0,
    project: str = "/repo",
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
        rows = [session(index, tool="codex-cli", age_days=index * 2) for index in range(10)]
        with patch.object(cli, "sessions_since", return_value=rows):
            result = cli.analyze_prompt(
                "Refactor the entire codebase",
                tool="codex",
                cwd="/repo",
            )

        impact = result["estimated_impact"]
        self.assertFalse(impact["available"])
        self.assertIn("cumulative", impact["basis"])

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

    def test_codex_prompt_gate_can_allow_original_prompt(self) -> None:
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
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
        payload = json.dumps({"prompt": "Refactor the entire codebase", "cwd": "/repo"})
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


if __name__ == "__main__":
    unittest.main()
