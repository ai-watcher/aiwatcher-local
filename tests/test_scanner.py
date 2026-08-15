from __future__ import annotations

import os
import tempfile
import unittest
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import scanner
from aiwatcher_cli.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    lookup,
)


class ProjectPathTests(unittest.TestCase):
    def test_surface_coverage_marks_detected_unsupported_tools_honestly(self) -> None:
        with patch.object(
            scanner,
            "discover_tools",
            return_value={
                "claude-code": False,
                "codex-cli": False,
                "cursor": False,
                "cline": True,
                "windsurf": True,
            },
        ):
            coverage = {row.surface_id: row for row in scanner.surface_coverage([])}

        self.assertEqual(coverage["cline"].status, "unsupported")
        self.assertEqual(coverage["cline"].status_label, "Detected, not scanned")
        self.assertEqual(coverage["windsurf"].history, "Not scanned yet")
        self.assertIn("Detection is not coverage", coverage["windsurf"].detail)

    def test_surface_coverage_distinguishes_desktop_chat_from_code_tab(self) -> None:
        rows = [
            scanner.LocalSession(
                session_id="desktop",
                tool="claude-code",
                surface="desktop",
                tokens_in=10,
            )
        ]
        tools = {
            "claude-code": True,
            "codex-cli": False,
            "cursor": False,
            "cline": False,
            "windsurf": False,
        }
        # Without hook events: Desktop Code tab stays "limited" ([..])
        with (
            patch.object(scanner, "discover_tools", return_value=tools),
            patch.object(scanner, "recent_hook_events", return_value=[]),
        ):
            coverage_no_hooks = {row.surface_id: row for row in scanner.surface_coverage(rows)}

        self.assertEqual(coverage_no_hooks["claude-desktop-code"].status, "limited")
        self.assertEqual(coverage_no_hooks["claude-desktop-code"].session_count, 1)
        self.assertEqual(coverage_no_hooks["claude-desktop-chat"].status, "companion")
        self.assertIn("No verified local hook", coverage_no_hooks["claude-desktop-chat"].automatic_gate)

        # A bare "some claude hook fired" event is NOT proof this surface is
        # hooked -- it carries no session id, so it could have come from the CLI
        # while the Desktop tab went entirely ungated.
        with (
            patch.object(scanner, "discover_tools", return_value=tools),
            patch.object(scanner, "recent_hook_events", return_value=[{"tool": "claude", "event": "received"}]),
        ):
            coverage_bare = {row.surface_id: row for row in scanner.surface_coverage(rows)}

        self.assertEqual(coverage_bare["claude-desktop-code"].status, "limited")

    TOOLS = {
        "claude-code": True, "codex-cli": False, "cursor": False,
        "cline": False, "windsurf": False,
    }

    def _desktop_coverage(self, rows, hook_events):
        with (
            patch.object(scanner, "discover_tools", return_value=self.TOOLS),
            patch.object(scanner, "recent_hook_events", return_value=hook_events),
        ):
            coverage = {row.surface_id: row for row in scanner.surface_coverage(rows)}
        return coverage["claude-desktop-code"]

    @staticmethod
    def _desktop_session(session_id: str, last_message_at, updated_at=None):
        return scanner.LocalSession(
            session_id=session_id,
            tool="claude-code",
            surface="desktop",
            last_message_at=last_message_at,
            updated_at=updated_at or last_message_at,
        )

    def test_desktop_code_tab_is_ok_when_a_hook_fired_for_a_real_desktop_session(self) -> None:
        # Proof by session id, not by "a claude hook fired somewhere".
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = self._desktop_coverage(
            [self._desktop_session("sess-1", session_at)],
            [{"tool": "claude", "event": "received", "session_id": "sess-1",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "automatic")
        self.assertEqual(row.status_label, "Auto gate + history")
        self.assertIn("1 of 1", row.detail)

    def test_desktop_code_tab_reports_silent_when_work_happened_with_no_hook(self) -> None:
        # The state the tool could not express before: the hook is installed but
        # is not reaching this surface, so those prompts went through ungated.
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = self._desktop_coverage(
            [self._desktop_session("sess-1", session_at)],
            # Evidence exists from the same period, just never for this surface.
            [{"tool": "codex", "event": "received", "session_id": "other",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "silent")
        self.assertEqual(row.status_label, "Hook not firing")
        self.assertIn("ungated", row.detail)

    def test_desktop_session_older_than_the_evidence_buffer_is_not_called_broken(self) -> None:
        # hook_events is a 50-entry ring buffer. A session predating everything
        # we still hold has no evidence either way -- "we forgot" must not be
        # reported as "the hook failed".
        row = self._desktop_coverage(
            [self._desktop_session("ancient", datetime(2026, 1, 1, tzinfo=timezone.utc))],
            [{"tool": "codex", "event": "received", "session_id": "other",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "limited")

    def test_mtime_refreshed_session_does_not_fake_recent_desktop_activity(self) -> None:
        # updated_at falls back to file mtime when a transcript ends in
        # untimestamped housekeeping lines, so a backup or re-index touching an
        # old file makes it read as active today. Judging by that would report a
        # healthy machine as broken. Only last_message_at may decide recency.
        stale = self._desktop_session(
            "touched-but-old",
            last_message_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        )
        row = self._desktop_coverage(
            [stale],
            [{"tool": "codex", "event": "received", "session_id": "other",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "limited")

    def test_partial_hook_coverage_still_counts_as_working(self) -> None:
        # A session where no prompt was ever submitted fires no UserPromptSubmit,
        # so less-than-total coverage is normal rather than a fault.
        base = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = self._desktop_coverage(
            [self._desktop_session("sess-1", base), self._desktop_session("sess-2", base)],
            [{"tool": "claude", "event": "received", "session_id": "sess-1",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "automatic")
        self.assertIn("1 of 2", row.detail)

    def test_desktop_status_survives_a_burst_of_other_tool_hook_events(self) -> None:
        # The regression this replaces: coverage used to ask "is there a claude
        # event in the last 50?", so a run of Codex work evicted the Claude
        # evidence and silently downgraded a working surface.
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        events = [{"tool": "claude", "event": "received", "session_id": "sess-1",
                   "created_at": "2026-08-14T11:00:00+00:00"}]
        events += [
            {"tool": "codex", "event": "received", "session_id": f"codex-{i}",
             "created_at": "2026-08-14T11:30:00+00:00"}
            for i in range(49)
        ]
        row = self._desktop_coverage([self._desktop_session("sess-1", session_at)], events)
        self.assertEqual(row.status, "automatic")

    def _cli_coverage(self, rows, hook_events):
        with (
            patch.object(scanner, "discover_tools", return_value=self.TOOLS),
            patch.object(scanner, "recent_hook_events", return_value=hook_events),
        ):
            coverage = {row.surface_id: row for row in scanner.surface_coverage(rows)}
        return coverage["claude-code-cli"]

    def test_cli_row_does_not_claim_automatic_from_a_directory_existing(self) -> None:
        # The over-claim: discover_tools() only reports that ~/.claude/projects
        # exists, which proves Claude Code ran here once -- not that a hook is
        # installed, trusted, or firing. Uninstall the hook and this row used to
        # keep reporting full automatic protection.
        row = self._cli_coverage([], [])
        self.assertEqual(row.status, "limited")
        self.assertEqual(row.status_label, "Hook-capable, verify locally")

    def test_cli_row_is_ok_once_a_hook_fired_for_a_real_cli_session(self) -> None:
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        cli_session = scanner.LocalSession(
            session_id="cli-1", tool="claude-code", surface="cli",
            last_message_at=session_at, updated_at=session_at,
        )
        row = self._cli_coverage(
            [cli_session],
            [{"tool": "claude", "event": "received", "session_id": "cli-1",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "automatic")
        self.assertIn("1 of 1", row.detail)

    def test_cli_row_with_no_recent_sessions_reads_as_unproven_not_broken(self) -> None:
        # No recent CLI work to check is not the same as a hook that stopped
        # working, and must not be reported with the same alarm.
        row = self._cli_coverage(
            [],
            [{"tool": "codex", "event": "received", "session_id": "other",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "limited")
        self.assertIn("unproven rather than broken", row.detail)

    def test_cli_row_counts_only_cli_sessions_not_every_claude_session(self) -> None:
        # It reported every claude-code session, so a machine used entirely
        # through the Desktop tab showed a large CLI session count.
        base = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        rows = [
            self._desktop_session("d1", base), self._desktop_session("d2", base),
            scanner.LocalSession(session_id="c1", tool="claude-code", surface="cli",
                                 last_message_at=base, updated_at=base),
        ]
        row = self._cli_coverage(rows, [])
        self.assertEqual(row.session_count, 1)

    def _codex_coverage(self, rows, hook_events):
        tools = dict(self.TOOLS, **{"codex-cli": True})
        with (
            patch.object(scanner, "discover_tools", return_value=tools),
            patch.object(scanner, "recent_hook_events", return_value=hook_events),
        ):
            coverage = {row.surface_id: row for row in scanner.surface_coverage(rows)}
        return coverage["codex-desktop"]

    @staticmethod
    def _codex_desktop_session(session_id: str, last_message_at):
        return scanner.LocalSession(
            session_id=session_id, tool="codex-cli", surface="desktop",
            last_message_at=last_message_at, updated_at=last_message_at,
        )

    def test_codex_desktop_needs_a_matching_session_not_just_any_codex_event(self) -> None:
        # The regression: this row asked "is there any codex hook event?", which
        # test-written events with no session id were satisfying -- so it claimed
        # a working hook on the strength of fixture data.
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = self._codex_coverage(
            [self._codex_desktop_session("codex-1", session_at)],
            [{"tool": "codex", "event": "received", "session_id": None,
              "cwd": "/repo", "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status, "silent")
        self.assertEqual(row.status_label, "Hook not firing")

    def test_codex_desktop_is_proven_by_a_matching_session_id(self) -> None:
        session_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = self._codex_coverage(
            [self._codex_desktop_session("codex-1", session_at)],
            [{"tool": "codex", "event": "received", "session_id": "codex-1",
              "created_at": "2026-08-14T11:00:00+00:00"}],
        )
        self.assertEqual(row.status_label, "Hook active + history")
        self.assertIn("1 of 1", row.detail)
        # Tops out at "limited", never "automatic": Codex token totals are
        # cumulative and subscription-based, so history stays weaker than
        # Claude's however well the gate is proven to work.
        self.assertEqual(row.status, "limited")

    def test_codex_rollout_sessions_carry_a_content_derived_timestamp(self) -> None:
        # Without last_message_at every Codex session is unjudgeable by
        # hook_liveness, so the coverage row above could never leave
        # "unverified" however well the hook was working.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            rollout = root / "rollout-session-1.jsonl"
            rows = [
                {"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "session-1", "cwd": temp_dir, "originator": "codex_desktop"}},
                {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    "last_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                }}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [root]):
                sessions, _events = scanner.scan_codex_rollouts()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].surface, "desktop")
        self.assertEqual(
            sessions[0].last_message_at,
            datetime(2026, 7, 1, 10, 0, 2, tzinfo=timezone.utc),
        )

    def test_decode_claude_path_preserves_hyphenated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "my-project"
            project.mkdir()
            if os.name == "nt":
                encoded = str(project).replace(":", "-").replace("\\", "-")
            else:
                encoded = "-" + str(project).lstrip("/").replace("/", "-")
            self.assertEqual(scanner._decode_claude_project_path(encoded), str(project))

    def test_choose_project_prefers_cost_then_event_count(self) -> None:
        normalized = {
            "/repo/a": "/repo/a",
            "/repo/b": "/repo/b",
            "/fallback": "/fallback",
        }
        with patch.object(scanner, "_normalize_project_path", side_effect=lambda value: normalized.get(value)):
            selected = scanner._choose_project_path(
                "/fallback",
                {"/repo/a": 20, "/repo/b": 3},
                {"/repo/a": 1.0, "/repo/b": 5.0},
            )
        self.assertEqual(selected, "/repo/b")

    def test_choose_project_uses_user_path_hint_when_cwd_is_unusable(self) -> None:
        """Hints decide attribution when no cwd resolves to a real project.

        This is the case the hint feature exists for: cwd pointing at `/`, AI
        tool storage, a temp dir, or a generic home folder, all of which
        normalize to None.
        """
        normalized = {"/repo/right": "/repo/right"}
        with patch.object(scanner, "_normalize_project_path", side_effect=lambda value: normalized.get(value)):
            selected = scanner._choose_project_path(
                "/unusable",
                {"/unusable": 25},
                {"/unusable": 10.0},
                {"/repo/right": 1},
                {"/repo/right": 0.0},
            )

        self.assertEqual(selected, "/repo/right")

    def test_choose_project_keeps_observed_cwd_over_incidental_path_mention(self) -> None:
        """An incidental path in a prompt must not outrank observed cwd.

        Prompts mention paths for all sorts of reasons -- "does the setting in
        /etc/nginx/nginx.conf matter here?" says nothing about which project the
        session belongs to. cwd is recorded on every event, so it is the far
        stronger signal whenever it resolves at all.
        """
        normalized = {
            "/repo/real": "/repo/real",
            "/etc/nginx": "/etc/nginx",
        }
        with patch.object(scanner, "_normalize_project_path", side_effect=lambda value: normalized.get(value)):
            selected = scanner._choose_project_path(
                "/repo/real",
                {"/repo/real": 500},
                {"/repo/real": 42.0},
                {"/etc/nginx": 1},
                {"/etc/nginx": 0.0},
            )

        self.assertEqual(selected, "/repo/real")

    def test_choose_project_allows_explicit_workspace_transition_to_override_stale_cwd(self) -> None:
        normalized = {
            "/repo/stale": "/repo/stale",
            "/repo/target": "/repo/target",
        }
        with patch.object(scanner, "_normalize_project_path", side_effect=lambda value: normalized.get(value)):
            selected = scanner._choose_project_path(
                "/repo/stale",
                {"/repo/stale": 500},
                {"/repo/stale": 42.0},
                {"/repo/target": 1},
                {"/repo/target": 0.0},
                {"/repo/target": 1},
            )

        self.assertEqual(selected, "/repo/target")

    def test_project_root_is_not_treated_as_project(self) -> None:
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertIsNone(scanner._normalize_project_path(str(Path("/").resolve())))
        self.assertEqual(scanner._choose_project_path(str(Path("/").resolve()), {}, {}), "unknown")

    def test_tool_storage_path_is_not_treated_as_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir) / ".claude" / "projects"
            log_project = storage_root / "-Users-chandrakala"
            log_project.mkdir(parents=True)
            scanner.PROJECT_PATH_CACHE.clear()
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [storage_root]):
                self.assertIsNone(scanner._normalize_project_path(str(log_project)))
                self.assertEqual(scanner._choose_project_path(str(log_project), {str(log_project): 2}, {str(log_project): 9.0}), "unknown")

    def test_claude_task_temp_path_is_not_treated_as_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_path = Path(temp_dir) / "claude-501" / "-Users-chandrakala" / "session-id" / "tasks"
            task_path.mkdir(parents=True)
            scanner.PROJECT_PATH_CACHE.clear()
            self.assertIsNone(scanner._normalize_project_path(str(task_path)))
            self.assertEqual(scanner._choose_project_path(str(task_path), {str(task_path): 2}, {str(task_path): 9.0}), "unknown")

    def test_agent_worktree_rolls_up_to_the_repository_it_was_cut_from(self) -> None:
        """Throwaway agent worktrees must not rank as projects of their own.

        Claude Code runs isolated agents in `<repo>/.claude/worktrees/agent-*`
        and deletes the directory afterwards. Since it no longer exists, the
        git-root lookup cannot fold it back, so each one would otherwise split
        cost off the real repo into an entry nobody can open.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "my-repo"
            repo.mkdir()
            worktree = repo / ".claude" / "worktrees" / "agent-a1d5324e6133bcc34"
            scanner.PROJECT_PATH_CACHE.clear()

            # Deleted worktree: the path no longer exists, which is the norm.
            self.assertEqual(scanner._normalize_project_path(str(worktree)), str(repo.resolve()))

            # A worktree still on disk folds back the same way.
            worktree.mkdir(parents=True)
            scanner.PROJECT_PATH_CACHE.clear()
            self.assertEqual(scanner._normalize_project_path(str(worktree)), str(repo.resolve()))

    def test_agent_worktree_under_home_does_not_become_a_project(self) -> None:
        """Rolling up must not smuggle a junk owner past the usual checks."""
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertIsNone(
            scanner._normalize_project_path(str(Path.home() / ".claude" / "worktrees" / "agent-abc123"))
        )

    def test_agent_scratchpad_under_temp_is_not_a_project(self) -> None:
        """The scratchpad root is named `claude` exactly -- no trailing hyphen."""
        temp_root = next(iter(scanner.TEMP_DIRS))
        scratch = temp_root / "claude" / "C--Users-dev-repo" / "session-id" / "scratchpad"
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertTrue(scanner._is_agent_scratch_path(scratch))
        self.assertIsNone(scanner._normalize_project_path(str(scratch)))

    def test_ordinary_temp_subdirectory_is_still_a_real_project(self) -> None:
        """Working out of a temp checkout is legitimate; only agent dirs are junk.

        Guards against fixing the scratchpad case by rejecting everything under
        a temp root, which would erase real work from the Projects page.
        """
        temp_root = next(iter(scanner.TEMP_DIRS))
        workspace = temp_root / "aiwatcher-fast-ui"
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertFalse(scanner._is_agent_scratch_path(workspace))
        self.assertEqual(scanner._normalize_project_path(str(workspace)), str(workspace))

    def test_tmp_root_and_claude_tmp_root_are_not_treated_as_projects(self) -> None:
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertIsNone(scanner._normalize_project_path("/private/tmp"))
        self.assertIsNone(scanner._normalize_project_path("/private/tmp/claude-501"))

    def test_common_user_folders_are_not_treated_as_projects(self) -> None:
        scanner.PROJECT_PATH_CACHE.clear()
        self.assertIsNone(scanner._normalize_project_path(str(Path.home() / "Downloads")))
        child_project = Path.home() / "Documents" / "example-project"
        self.assertEqual(scanner._normalize_project_path(str(child_project)), str(child_project))

    def test_project_hints_require_real_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "right-repo"
            project.mkdir()
            readme = project / "README.md"
            readme.write_text("demo", encoding="utf-8")

            hints = scanner._project_hints_from_text(f"Please work in `{readme}` before editing.")

        self.assertEqual(hints, [str(project.resolve())])

    def test_project_transition_hints_distinguish_instruction_from_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current"
            other = Path(temp_dir) / "other"
            current.mkdir()
            other.mkdir()

            explicit = scanner._intentional_project_hints_from_text(
                f"Continue the dashboard work in {other}."
            )
            incidental = scanner._intentional_project_hints_from_text(
                f"Does the setting in {other / 'config.toml'} matter here?"
            )

        self.assertEqual(explicit, [str(other.resolve())])
        self.assertEqual(incidental, [])

    def test_project_hints_ignore_unresolvable_tilde_user(self) -> None:
        self.assertEqual(scanner._project_hints_from_text("Please inspect ~est before changing code."), [])

    def test_project_hints_ignore_long_quoted_prose_that_starts_like_path(self) -> None:
        with patch.object(scanner.Path, "exists", side_effect=AssertionError("prose should not touch filesystem")):
            hints = scanner._project_hints_from_text('Review "/private/tmp/repo and then read this long pasted transcript ' + ("x" * 600) + '"')

        self.assertEqual(hints, [])

    def test_absolute_path_pattern_keeps_full_windows_path(self) -> None:
        match = scanner._ABSOLUTE_PATH_RE.search(
            r"Please work in C:\Users\developer\projects\right-repo before editing."
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.group("plain"), r"C:\Users\developer\projects\right-repo")

    def test_codex_rollout_uses_measured_token_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            rollout = root / "rollout-session-1.jsonl"
            rows = [
                {"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta", "payload": {"id": "session-1", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.2-codex", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    "last_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                }}},
                {"timestamp": "2026-07-01T10:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1800, "output_tokens": 200, "total_tokens": 2000},
                    "last_token_usage": {"input_tokens": 800, "output_tokens": 100, "total_tokens": 900},
                }}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [root]):
                sessions, events = scanner.scan_codex_rollouts()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].tokens_in, 1800)
        self.assertEqual(sessions[0].tokens_out, 200)
        self.assertEqual(sessions[0].agent_calls, 2)
        self.assertEqual(len(events), 2)
        self.assertNotIn("cumulative", " ".join(sessions[0].notes).lower())

    def test_codex_window_scan_reads_recent_tail_of_large_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rollout = Path(temp_dir) / "rollout-large.jsonl"
            old_line = json.dumps({
                "timestamp": "2026-05-01T10:00:00Z",
                "type": "response_item",
                "payload": {"role": "assistant", "content": "old" * 400},
            })
            recent_line = json.dumps({
                "timestamp": "2026-08-09T10:00:00Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                    "last_token_usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                }},
            })
            rollout.write_text("\n".join([old_line] * 40 + [recent_line]) + "\n", encoding="utf-8")
            since = datetime(2026, 8, 8, tzinfo=timezone.utc)
            with (
                patch.object(scanner, "CODEX_TAIL_MIN_FILE_BYTES", 1),
                patch.object(scanner, "CODEX_TAIL_INITIAL_BYTES", len(recent_line) + 20),
                patch.object(scanner, "CODEX_TAIL_MAX_BYTES", len(recent_line) + 80),
            ):
                lines = [line for _, line in scanner._codex_rollout_lines(rollout, since)]

        self.assertTrue(lines)
        self.assertTrue(any("2026-08-09T10:00:00Z" in line for line in lines))
        self.assertFalse(any("2026-05-01T10:00:00Z" in line for line in lines))
        self.assertTrue(scanner._codex_window_line_is_essential(recent_line))
        self.assertFalse(scanner._codex_window_line_is_essential(old_line))

    def _write_claude_session(self, temp_dir: str, cwd: str, prompt: str) -> Path:
        projects = Path(temp_dir) / "projects"
        if os.name == "nt":
            encoded = cwd.replace(":", "-").replace("\\", "-")
        else:
            encoded = "-" + cwd.lstrip("/").replace("/", "-")
        project_dir = projects / encoded
        project_dir.mkdir(parents=True)
        rows = [
            {
                "type": "user",
                "timestamp": "2026-07-12T10:00:00Z",
                "cwd": cwd,
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-12T10:00:01Z",
                "cwd": cwd,
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            },
        ]
        (project_dir / "sess.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        return projects

    def test_claude_user_path_hint_used_when_recorded_cwd_is_unusable(self) -> None:
        """The home directory does not resolve to a project, so the hint decides."""
        with tempfile.TemporaryDirectory() as temp_dir:
            right_repo = Path(temp_dir) / "right-repo"
            right_repo.mkdir()
            scanner.PROJECT_PATH_CACHE.clear()
            projects = self._write_claude_session(
                temp_dir,
                str(Path.home()),
                f"Fix the attribution in {right_repo}.",
            )

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()
                events = scanner.scan_claude_code_events()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].project_path, str(right_repo.resolve()))
        self.assertTrue(events)
        self.assertTrue(all(event.project_path == str(right_repo.resolve()) for event in events))

    def test_claude_incidental_path_mention_does_not_move_session_off_real_cwd(self) -> None:
        """Mentioning an unrelated path in passing must not re-attribute the session."""
        with tempfile.TemporaryDirectory() as temp_dir:
            real_repo = Path(temp_dir) / "real-repo"
            unrelated = Path(temp_dir) / "etc" / "nginx"
            real_repo.mkdir()
            unrelated.mkdir(parents=True)
            (unrelated / "nginx.conf").write_text("worker_processes 1;", encoding="utf-8")
            scanner.PROJECT_PATH_CACHE.clear()
            projects = self._write_claude_session(
                temp_dir,
                str(real_repo),
                f"quick q: does the setting in {unrelated / 'nginx.conf'} matter here?",
            )

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()
                events = scanner.scan_claude_code_events()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].project_path, str(real_repo.resolve()))
        self.assertTrue(events)
        self.assertTrue(all(event.project_path == str(real_repo.resolve()) for event in events))

    def test_claude_explicit_workspace_transition_overrides_stale_real_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stale_repo = Path(temp_dir) / "stale-repo"
            target_repo = Path(temp_dir) / "target-repo"
            stale_repo.mkdir()
            target_repo.mkdir()
            scanner.PROJECT_PATH_CACHE.clear()
            projects = self._write_claude_session(
                temp_dir,
                str(stale_repo),
                f"Continue the AIWatcher UI work in {target_repo}.",
            )

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()
                events = scanner.scan_claude_code_events()

        self.assertEqual(sessions[0].project_path, str(target_repo.resolve()))
        self.assertTrue(events)
        self.assertTrue(all(event.project_path == str(target_repo.resolve()) for event in events))

    def _run_codex_rollout(self, temp_dir: str, cwd: str, prompt: str):
        root = Path(temp_dir) / "sessions"
        root.mkdir()
        rows = [
            {
                "timestamp": "2026-07-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-1", "cwd": cwd, "originator": "codex-tui"},
            },
            {
                "timestamp": "2026-07-01T10:00:01Z",
                "type": "response_item",
                "payload": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
            {
                "timestamp": "2026-07-01T10:00:02Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5", "cwd": cwd},
            },
            {
                "timestamp": "2026-07-01T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                        "last_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    },
                },
            },
        ]
        (root / "rollout-session-1.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
        )
        scanner.PROJECT_PATH_CACHE.clear()
        with patch.object(scanner, "CODEX_SESSIONS_DIRS", [root]):
            scanner.CODEX_ROLLOUT_CACHE = None
            result = scanner.scan_codex_rollouts()
        scanner.CODEX_ROLLOUT_CACHE = None
        return result

    def test_codex_user_path_hint_used_when_recorded_cwd_is_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            right_repo = Path(temp_dir) / "right-repo"
            right_repo.mkdir()
            sessions, events = self._run_codex_rollout(
                temp_dir,
                str(Path.home()),
                f"Continue the AIWatcher UI work in {right_repo}.",
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].project_path, str(right_repo.resolve()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].project_path, str(right_repo.resolve()))

    def test_codex_incidental_path_mention_does_not_move_session_off_real_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_repo = Path(temp_dir) / "real-repo"
            unrelated = Path(temp_dir) / "etc" / "nginx"
            real_repo.mkdir()
            unrelated.mkdir(parents=True)
            (unrelated / "nginx.conf").write_text("worker_processes 1;", encoding="utf-8")
            sessions, events = self._run_codex_rollout(
                temp_dir,
                str(real_repo),
                f"quick q: does the setting in {unrelated / 'nginx.conf'} matter here?",
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].project_path, str(real_repo.resolve()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].project_path, str(real_repo.resolve()))

    def test_codex_explicit_workspace_transition_overrides_stale_real_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stale_repo = Path(temp_dir) / "stale-repo"
            target_repo = Path(temp_dir) / "target-repo"
            stale_repo.mkdir()
            target_repo.mkdir()
            sessions, events = self._run_codex_rollout(
                temp_dir,
                str(stale_repo),
                f"Continue the AIWatcher UI work in {target_repo}.",
            )

        self.assertEqual(sessions[0].project_path, str(target_repo.resolve()))
        self.assertEqual(events[0].project_path, str(target_repo.resolve()))


class PromptCacheAccountingTests(unittest.TestCase):
    """Anthropic reports `input_tokens` as the *uncached remainder*. Reading it
    alone ignored the cached bulk of every prompt and understated real cost by
    roughly 11x across this machine's own history."""

    # The fixture line below is stamped with this date, and spend is priced at
    # the rate in effect when it happened -- so the expected figures are derived
    # from the table at that date rather than written in as dollars. Otherwise
    # these assertions would encode whichever rate happened to be current when
    # they were written and quietly go wrong when one lapses.
    SCANNED_AT = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)

    def _rates(self) -> tuple[float, float]:
        pricing = lookup("claude-sonnet-5", self.SCANNED_AT)
        assert pricing is not None
        return float(pricing["in"]), float(pricing["out"])

    def _scan_one(self, usage: dict) -> scanner.LocalSession:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            (project / "sess.jsonl").write_text(
                json.dumps({
                    "type": "assistant",
                    "timestamp": "2026-06-24T10:00:00Z",
                    "message": {"model": "claude-sonnet-5", "usage": usage},
                }) + "\n",
                encoding="utf-8",
            )
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    def test_cached_tokens_are_counted_and_billed(self) -> None:
        # Verbatim usage shape from a real local log.
        session = self._scan_one({
            "input_tokens": 2,
            "cache_creation_input_tokens": 13_099,
            "cache_read_input_tokens": 33_775,
            "output_tokens": 339,
            "cache_creation": {"ephemeral_1h_input_tokens": 13_099, "ephemeral_5m_input_tokens": 0},
        })
        self.assertEqual(session.tokens_in, 2 + 13_099 + 33_775)
        self.assertEqual(session.cache_read_tokens, 33_775)
        self.assertEqual(session.cache_write_tokens, 13_099)
        price_in, price_out = self._rates()
        expected = (
            2 * price_in
            + 13_099 * price_in * CACHE_WRITE_1H_MULTIPLIER
            + 33_775 * price_in * CACHE_READ_MULTIPLIER
            + 339 * price_out
        ) / 1_000_000
        self.assertAlmostEqual(session.cost_usd, expected, places=9)
        # The defect: reading input_tokens alone would price 2 tokens, not 46,876.
        self.assertGreater(session.cost_usd, 2 * price_in / 1_000_000 * 1_000)

    def test_cache_counters_are_a_subset_of_tokens_in_not_an_addition(self) -> None:
        session = self._scan_one({
            "input_tokens": 100,
            "cache_read_input_tokens": 900,
            "output_tokens": 10,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        })
        self.assertEqual(session.tokens_in, 1_000)
        self.assertLessEqual(session.cache_read_tokens + session.cache_write_tokens, session.tokens_in)

    def test_missing_ttl_breakdown_falls_back_to_the_cheaper_5m_rate(self) -> None:
        # Older logs report only the combined creation total. Attributing it to
        # the 1h bucket would overstate cost, so it lands in 5m instead.
        session = self._scan_one({
            "input_tokens": 0,
            "cache_creation_input_tokens": 1_000_000,
            "output_tokens": 0,
        })
        self.assertEqual(session.cache_write_tokens, 1_000_000)
        price_in, _ = self._rates()
        self.assertAlmostEqual(session.cost_usd, price_in * CACHE_WRITE_5M_MULTIPLIER, places=9)
        # The point of the fallback: the 1h bucket would have cost more.
        self.assertLess(session.cost_usd, price_in * CACHE_WRITE_1H_MULTIPLIER)

    def test_uncached_session_is_unchanged(self) -> None:
        session = self._scan_one({"input_tokens": 1_000, "output_tokens": 500})
        self.assertEqual(session.tokens_in, 1_000)
        self.assertEqual(session.cache_read_tokens, 0)
        self.assertEqual(session.cache_write_tokens, 0)
        price_in, price_out = self._rates()
        self.assertAlmostEqual(
            session.cost_usd, (1_000 * price_in + 500 * price_out) / 1_000_000, places=9
        )

    def test_codex_cached_tokens_are_not_double_counted(self) -> None:
        # Codex uses the opposite convention: its `input_tokens` already
        # INCLUDES `cached_input_tokens`, so the Anthropic fix must not also
        # add them there or the same tokens get counted twice.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            rows = [
                {"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "session-1", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:01Z", "type": "turn_context",
                 "payload": {"model": "gpt-5.2-codex", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg", "payload": {
                    "type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 16_225, "cached_input_tokens": 11_008,
                                              "output_tokens": 165, "total_tokens": 16_390},
                        "last_token_usage": {"input_tokens": 16_225, "cached_input_tokens": 11_008,
                                             "output_tokens": 165, "total_tokens": 16_390},
                    }}},
            ]
            (root / "rollout-session-1.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [root]):
                scanner.CODEX_ROLLOUT_CACHE = None
                sessions, _events = scanner.scan_codex_rollouts()
                scanner.CODEX_ROLLOUT_CACHE = None

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].tokens_in, 16_225)


class PerRequestUsageTests(unittest.TestCase):
    """Claude Code writes one transcript line per *content block*, each
    repeating the same message-level usage. Usage is reported per API request,
    so summing per line counted one request's tokens once per block -- locally
    that inflated a single session from $71.57 to $127.19, and was what made
    AIWatcher disagree with Claude Code's own `/cost`.
    """

    USAGE = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": 100_000,
        "output_tokens": 500,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000, "ephemeral_5m_input_tokens": 0},
    }

    def _write(self, project: Path, lines: list[dict]) -> None:
        (project / "sess.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )

    def _scan(self, lines: list[dict]):
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            self._write(project, lines)
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                return scanner.scan_claude_code()[0], scanner.scan_claude_code_events()

    def _line(self, *, request_id=None, message_id=None, block="text"):
        message = {"model": "claude-sonnet-5", "usage": self.USAGE,
                   "content": [{"type": block, "text": "x"}]}
        if message_id is not None:
            message["id"] = message_id
        line = {"type": "assistant", "timestamp": "2026-06-24T10:00:00Z", "message": message}
        if request_id is not None:
            line["requestId"] = request_id
        return line

    def test_one_request_across_four_lines_is_billed_once(self) -> None:
        four = [self._line(request_id="req_1", message_id="msg_1") for _ in range(4)]
        session, events = self._scan(four)

        self.assertEqual(session.tokens_in, 10 + 1_000 + 100_000)
        self.assertEqual(session.tokens_out, 500)
        self.assertEqual(session.cache_read_tokens, 100_000)
        # Every line is still a real content block, so every line is an event --
        # only the duplicated bill is dropped.
        self.assertEqual(len(events), 4)
        self.assertEqual(sum(e.tokens_in for e in events), 10 + 1_000 + 100_000)
        self.assertAlmostEqual(sum(e.cost_usd for e in events), session.cost_usd, places=9)

    def test_distinct_requests_are_billed_separately(self) -> None:
        session, _ = self._scan([
            self._line(request_id="req_1", message_id="msg_1"),
            self._line(request_id="req_2", message_id="msg_2"),
        ])
        self.assertEqual(session.tokens_in, 2 * (10 + 1_000 + 100_000))
        self.assertEqual(session.tokens_out, 2 * 500)

    def test_message_id_is_the_fallback_when_request_id_is_absent(self) -> None:
        # Older transcripts predate requestId.
        session, _ = self._scan([self._line(message_id="msg_1") for _ in range(3)])
        self.assertEqual(session.tokens_in, 10 + 1_000 + 100_000)

    def test_lines_with_no_identifier_are_each_counted(self) -> None:
        # Safe direction: a missed dedup over-counts by one, while merging on a
        # bad key would silently discard a real request.
        session, _ = self._scan([self._line() for _ in range(2)])
        self.assertEqual(session.tokens_in, 2 * (10 + 1_000 + 100_000))

    def test_sessions_and_events_agree_on_the_same_transcript(self) -> None:
        lines = [
            self._line(request_id="req_1", message_id="msg_1"),
            self._line(request_id="req_1", message_id="msg_1", block="tool_use"),
            self._line(request_id="req_2", message_id="msg_2"),
        ]
        session, events = self._scan(lines)
        self.assertEqual(sum(e.tokens_in for e in events), session.tokens_in)
        self.assertEqual(sum(e.tokens_out for e in events), session.tokens_out)
        self.assertAlmostEqual(sum(e.cost_usd for e in events), session.cost_usd, places=9)


class ScanDateTests(unittest.TestCase):
    def test_updated_at_uses_mtime_when_tail_events_lack_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-06-24T10:00:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"queue-operation"}',
                '{"type":"attachment"}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            later = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(session_file, (later, later))

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertIsNotNone(session.updated_at)
            self.assertEqual(session.updated_at.astimezone(timezone.utc).date(), date(2026, 6, 30))
            self.assertIsNotNone(session.started_at)
            self.assertEqual(session.started_at.astimezone(timezone.utc).date(), date(2026, 6, 24))

    def test_updated_at_ignores_mtime_when_fully_timestamped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-06-24T10:00:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"assistant","timestamp":"2026-06-24T10:05:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # Simulate the file being copied/restored/synced well after the session ended.
            later = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(session_file, (later, later))

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertIsNotNone(session.updated_at)
            self.assertEqual(session.updated_at.astimezone(timezone.utc).date(), date(2026, 6, 24))


class ModelAttributionTests(unittest.TestCase):
    """A session that switches models mid-conversation must keep every model's
    usage visible, instead of the earlier behavior where `model = event_model`
    silently overwrote the session's model field on every event, so only the
    last model used in the file survived into the session summary."""

    def test_multi_model_session_keeps_every_model_in_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-fable-5","usage":{"input_tokens":1000,"output_tokens":200}}}',
                '{"type":"assistant","timestamp":"2026-07-12T10:05:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":2000,"output_tokens":400}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            # Fable was used first but Sonnet used more tokens — Sonnet should be
            # the reported primary model, but Fable must not disappear entirely.
            self.assertEqual(session.model, "claude-sonnet-4-6")
            self.assertIn("claude-fable-5", session.model_breakdown)
            self.assertIn("claude-sonnet-4-6", session.model_breakdown)
            self.assertEqual(session.model_breakdown["claude-fable-5"]["tokens_in"], 1000)
            self.assertEqual(session.model_breakdown["claude-sonnet-4-6"]["tokens_in"], 2000)
            # Session-level totals still sum across every model used.
            self.assertEqual(session.tokens_in, 3000)

    def test_desktop_entrypoint_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(sessions[0].surface, "desktop")

    def test_cli_entrypoint_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"cli",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(sessions[0].surface, "cli")

    def test_missing_entrypoint_leaves_surface_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertIsNone(sessions[0].surface)

    def test_model_usage_totals_aggregates_across_sessions_and_falls_back(self) -> None:
        with_breakdown = scanner.LocalSession(
            session_id="a",
            tool="claude-code",
            model="claude-sonnet-4-6",
            tokens_in=100,
            tokens_out=50,
            cost_usd=1.0,
            model_breakdown={
                "claude-fable-5": {"tokens_in": 40, "tokens_out": 20, "cost_usd": 0.4, "agent_calls": 1, "tool_calls": 0},
                "claude-sonnet-4-6": {"tokens_in": 60, "tokens_out": 30, "cost_usd": 0.6, "agent_calls": 1, "tool_calls": 1},
            },
        )
        # Older/other-tool sessions with no breakdown fall back to their single model field.
        without_breakdown = scanner.LocalSession(
            session_id="b",
            tool="cursor",
            model="cursor-ai",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.1,
            agent_calls=1,
        )

        totals = scanner.model_usage_totals([with_breakdown, without_breakdown])

        self.assertEqual(totals["claude-fable-5"]["tokens_in"], 40)
        self.assertEqual(totals["claude-sonnet-4-6"]["tokens_in"], 60)
        self.assertEqual(totals["cursor-ai"]["tokens_in"], 10)
        self.assertEqual(totals["claude-fable-5"]["sessions"], 1)

    def test_display_model_name_relabels_synthetic_rate_limit_marker(self) -> None:
        """"<synthetic>" is Claude's own marker for a client-injected message
        (e.g. a rate-limit notice) with zero tokens/cost — not a real model.
        The raw model_breakdown key stays "<synthetic>"; only the label shown
        to the user changes."""
        self.assertEqual(scanner.display_model_name("<synthetic>"), "Session limit model")
        self.assertEqual(scanner.display_model_name("claude-sonnet-4-6"), "claude-sonnet-4-6")
        self.assertEqual(scanner.display_model_name(None), "unknown")


class CodexOriginatorTests(unittest.TestCase):
    def test_codex_desktop_originator_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            day_dir = sessions_dir / "2026" / "07" / "12"
            day_dir.mkdir(parents=True)
            rollout = day_dir / "rollout-test.jsonl"
            lines = [
                json.dumps({
                    "type": "session_meta",
                    "timestamp": "2026-07-12T10:00:00Z",
                    "payload": {"id": "codex-1", "cwd": "/repo", "originator": "Codex Desktop"},
                }),
                json.dumps({
                    "type": "turn_context",
                    "timestamp": "2026-07-12T10:00:01Z",
                    "payload": {"cwd": "/repo", "model": "gpt-5.5"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-07-12T10:00:02Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
                            "last_token_usage": {"input_tokens": 80, "output_tokens": 20},
                        },
                    },
                }),
            ]
            rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]):
                scanner.CODEX_ROLLOUT_CACHE = None
                sessions, _events = scanner.scan_codex_rollouts()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].surface, "desktop")

    def test_codex_tui_originator_is_recorded_as_cli_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            day_dir = sessions_dir / "2026" / "07" / "12"
            day_dir.mkdir(parents=True)
            rollout = day_dir / "rollout-test.jsonl"
            lines = [
                json.dumps({
                    "type": "session_meta",
                    "timestamp": "2026-07-12T10:00:00Z",
                    "payload": {"id": "codex-2", "cwd": "/repo", "originator": "codex-tui"},
                }),
                json.dumps({
                    "type": "turn_context",
                    "timestamp": "2026-07-12T10:00:01Z",
                    "payload": {"cwd": "/repo", "model": "gpt-5.5"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-07-12T10:00:02Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
                            "last_token_usage": {"input_tokens": 80, "output_tokens": 20},
                        },
                    },
                }),
            ]
            rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]):
                scanner.CODEX_ROLLOUT_CACHE = None
                sessions, _events = scanner.scan_codex_rollouts()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].surface, "cli")


if __name__ == "__main__":
    unittest.main()


class ClipSessionsToWindowTests(unittest.TestCase):
    """Windows used to be all-or-nothing on updated_at: a session touched once
    inside a window contributed every dollar it had ever cost. On long-running
    sessions that roughly doubled the reported total."""

    def _session(self, sid="s1", tool="claude-code", **kw):
        base = dict(
            session_id=sid, tool=tool, project_path="/repo",
            started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
            model="claude-sonnet-5", tokens_in=1_000, tokens_out=100, cost_usd=10.0,
        )
        base.update(kw)
        return scanner.LocalSession(**base)

    def _event(self, when, *, sid="s1", cost=1.0, tokens_in=100, cache_read=0,
               event_type="assistant", model="claude-sonnet-5"):
        return scanner.LocalEvent(
            event_id=f"{sid}-{when.isoformat()}", session_id=sid, tool="claude-code",
            event_type=event_type, timestamp=when, project_path="/repo", model=model,
            tokens_in=tokens_in, tokens_out=10, cache_read_tokens=cache_read, cost_usd=cost,
        )

    def test_only_in_window_spend_is_counted(self) -> None:
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        events = [
            self._event(datetime(2026, 6, 10, tzinfo=timezone.utc), cost=7.0),
            self._event(datetime(2026, 6, 25, tzinfo=timezone.utc), cost=3.0),
        ]
        clipped = scanner.clip_sessions_to_window([self._session()], events, since)

        self.assertEqual(len(clipped), 1)
        self.assertAlmostEqual(clipped[0].cost_usd, 3.0, places=6)
        self.assertEqual(clipped[0].tokens_in, 100)

    def test_session_with_no_in_window_events_is_dropped(self) -> None:
        # updated_at can come from file mtime, so a session can look recent
        # while every costed turn predates the window.
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        events = [self._event(datetime(2026, 6, 10, tzinfo=timezone.utc), cost=7.0)]
        self.assertEqual(scanner.clip_sessions_to_window([self._session()], events, since), [])

    def test_cache_counters_are_clipped_too(self) -> None:
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        events = [
            self._event(datetime(2026, 6, 10, tzinfo=timezone.utc), cache_read=9_000),
            self._event(datetime(2026, 6, 25, tzinfo=timezone.utc), cache_read=500),
        ]
        clipped = scanner.clip_sessions_to_window([self._session(cache_read_tokens=9_500)], events, since)
        self.assertEqual(clipped[0].cache_read_tokens, 500)

    def test_model_breakdown_is_recomputed_from_in_window_events(self) -> None:
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        events = [
            self._event(datetime(2026, 6, 10, tzinfo=timezone.utc), cost=5.0, model="claude-opus-5"),
            self._event(datetime(2026, 6, 25, tzinfo=timezone.utc), cost=2.0, model="claude-sonnet-5"),
        ]
        clipped = scanner.clip_sessions_to_window([self._session()], events, since)
        breakdown = clipped[0].model_breakdown

        self.assertEqual(set(breakdown), {"claude-sonnet-5"})
        self.assertAlmostEqual(breakdown["claude-sonnet-5"]["cost_usd"], 2.0, places=6)

    def test_tools_without_events_keep_whole_session_and_are_flagged(self) -> None:
        # Cursor and the Codex sqlite path emit sessions but no per-turn events.
        # Dropping them would erase a whole tool's contribution, so they keep
        # the old rule and say so rather than being silently overstated.
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        cursor = self._session(sid="c1", tool="cursor", cost_usd=4.0)
        clipped = scanner.clip_sessions_to_window([cursor], [], since)

        self.assertEqual(len(clipped), 1)
        self.assertAlmostEqual(clipped[0].cost_usd, 4.0, places=6)
        self.assertIn(scanner.CLIP_FALLBACK_NOTE, clipped[0].notes)

    def test_eventless_session_outside_the_window_is_still_excluded(self) -> None:
        since = datetime(2026, 7, 15, tzinfo=timezone.utc)
        cursor = self._session(sid="c1", tool="cursor")
        self.assertEqual(scanner.clip_sessions_to_window([cursor], [], since), [])

    def test_clipping_does_not_mutate_the_input_session(self) -> None:
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        original = self._session()
        events = [self._event(datetime(2026, 6, 25, tzinfo=timezone.utc), cost=3.0)]
        scanner.clip_sessions_to_window([original], events, since)

        self.assertAlmostEqual(original.cost_usd, 10.0, places=6)
        self.assertEqual(original.notes, [])

    def test_clipped_total_equals_the_raw_in_window_event_sum(self) -> None:
        since = datetime(2026, 6, 20, tzinfo=timezone.utc)
        events = [
            self._event(datetime(2026, 6, 21, tzinfo=timezone.utc), sid="a", cost=1.5),
            self._event(datetime(2026, 6, 22, tzinfo=timezone.utc), sid="b", cost=2.5),
            self._event(datetime(2026, 6, 1, tzinfo=timezone.utc), sid="b", cost=99.0),
        ]
        rows = [self._session(sid="a"), self._session(sid="b")]
        clipped = scanner.clip_sessions_to_window(rows, events, since)
        raw = sum(e.cost_usd for e in events if e.timestamp >= since)

        self.assertAlmostEqual(sum(r.cost_usd for r in clipped), raw, places=6)
