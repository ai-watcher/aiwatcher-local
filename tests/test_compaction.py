from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli import compaction
from aiwatcher_cli.scanner import LocalSession

SHA = "a" * 40
OLDER = "b" * 40
ZERO = "0" * 40


def write_reflog(repo: Path, lines: list[str], *, worktree_of: Path | None = None) -> None:
    """A repo with only the file compaction reads: .git/logs/HEAD.

    With `worktree_of`, `repo/.git` is the pointer file a linked worktree
    carries and the reflog lives under the pointed-to gitdir.
    """
    if worktree_of is None:
        git_dir = repo / ".git"
    else:
        git_dir = worktree_of / ".git" / "worktrees" / repo.name
        (repo / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "logs").mkdir(parents=True, exist_ok=True)
    (git_dir / "logs" / "HEAD").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reflog_line(old: str, new: str, when: datetime, action: str) -> str:
    return f"{old} {new} Dev <dev@example.com> {int(when.timestamp())} +0000\t{action}"


def prompt(*, when: datetime, text: str = "next thing please") -> str:
    return json.dumps({
        "type": "user",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {"role": "user", "content": text},
    })


def turn(*, when: datetime, context: int, files: tuple[str, ...] = (), model: str = "claude-sonnet-5") -> str:
    content = [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}} for path in files]
    return json.dumps({
        "type": "assistant",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {
            "model": model,
            "usage": {"input_tokens": 10, "output_tokens": 200, "cache_read_input_tokens": context - 10},
            "content": content,
        },
    })


class HeadCommitFromReflogTests(unittest.TestCase):
    """The boundary is read from the reflog, never from a git subprocess: this
    runs on the Companion's three-second poll."""

    def test_reads_the_last_commit_and_its_subject(self) -> None:
        when = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_reflog(repo, [
                reflog_line(ZERO, OLDER, when - timedelta(hours=1), "commit (initial): start"),
                reflog_line(OLDER, SHA, when, "commit: fix(companion): pin windows"),
            ])
            boundary = compaction.head_commit(str(repo))
        assert boundary is not None
        self.assertEqual(boundary.sha, SHA)
        self.assertEqual(boundary.subject, "fix(companion): pin windows")
        self.assertEqual(boundary.committed_at, when)

    def test_a_checkout_points_at_a_commit_made_earlier(self) -> None:
        # HEAD moved by checkout: the sha is the checkout's target, the subject
        # and time come from the line that created that sha.
        made = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_reflog(repo, [
                reflog_line(ZERO, SHA, made, "commit: the real one"),
                reflog_line(SHA, OLDER, made + timedelta(minutes=5), "checkout: moving from main to other"),
                reflog_line(OLDER, SHA, made + timedelta(minutes=9), "checkout: moving from other to main"),
            ])
            boundary = compaction.head_commit(str(repo))
        assert boundary is not None
        self.assertEqual(boundary.sha, SHA)
        self.assertEqual(boundary.subject, "the real one")
        self.assertEqual(boundary.committed_at, made)

    def test_a_linked_worktree_follows_its_gitdir_pointer(self) -> None:
        when = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp, "main")
            tree = Path(tmp, "feature")
            main.mkdir()
            tree.mkdir()
            write_reflog(tree, [reflog_line(ZERO, SHA, when, "commit: on the worktree")], worktree_of=main)
            boundary = compaction.head_commit(str(tree))
        assert boundary is not None
        self.assertEqual(boundary.subject, "on the worktree")

    def test_no_repo_or_no_commits_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(compaction.head_commit(tmp))
            (Path(tmp) / ".git" / "logs").mkdir(parents=True)
            (Path(tmp) / ".git" / "logs" / "HEAD").write_text("", encoding="utf-8")
            self.assertIsNone(compaction.head_commit(tmp))
        self.assertIsNone(compaction.head_commit(None))


class AssessTests(unittest.TestCase):
    """Every number in the verdict is observed: the commit from the reflog, the
    history to shed from the transcript, the floor from the session's own first
    call. There is no constant to tune."""

    COMMIT_AT = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)

    def _session(self, repo: str, transcript: str, *, tool: str = "claude-code", model: str = "claude-sonnet-5") -> LocalSession:
        return LocalSession(
            session_id="s1", tool=tool, project_path=repo, source_path=transcript, model=model,
            started_at=self.COMMIT_AT - timedelta(hours=1), updated_at=self.COMMIT_AT + timedelta(minutes=30),
        )

    def _build(self, tmp: str, before: list[int], after: list[tuple[int, tuple[str, ...]]], *, subject: str = "fix: thing", tool: str = "claude-code", model: str = "claude-sonnet-5") -> LocalSession:
        repo = Path(tmp)
        write_reflog(repo, [reflog_line(ZERO, SHA, self.COMMIT_AT, f"commit: {subject}")])
        lines = [
            turn(when=self.COMMIT_AT - timedelta(minutes=30 - index), context=context, model=model)
            for index, context in enumerate(before)
        ]
        # One typed prompt before the commit (not counted) and one per call after.
        lines.insert(0, prompt(when=self.COMMIT_AT - timedelta(minutes=45)))
        for index, (context, files) in enumerate(after):
            when = self.COMMIT_AT + timedelta(minutes=index + 1)
            lines.append(prompt(when=when - timedelta(seconds=30)))
            lines.append(turn(when=when, context=context, files=tuple(str(repo / f) for f in files), model=model))
        transcript = repo / "t.jsonl"
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self._session(str(repo), str(transcript), tool=tool, model=model)

    def test_a_commit_with_most_of_the_replay_behind_it_is_recommended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 150_000, 300_000], [(310_000, ("a.py",)), (320_000, ("a.py", "tests/test_a.py"))])
            result = compaction.assess(session)
        assert result is not None
        self.assertTrue(result.recommend)
        self.assertEqual(result.context_at_commit, 300_000)
        self.assertEqual(result.first_turn_tokens, 60_000)
        self.assertEqual(result.dead_tokens, 240_000)
        self.assertEqual(result.since_tokens, 20_000)
        self.assertEqual(result.after_estimate, 80_000)
        self.assertEqual(result.turns_since_commit, 2)
        self.assertEqual(result.prompts_since_commit, 2)
        self.assertEqual(result.files_since, ["a.py", "tests/test_a.py"])
        self.assertIn(SHA[:7], result.command)
        self.assertIn('"fix: thing"', result.command)
        self.assertIn("a.py, tests/test_a.py", result.command)
        self.assertTrue(result.command.startswith("/compact "))

    def test_the_floor_is_the_sessions_own_first_context(self) -> None:
        # 40K of finished history against a 60K fresh context: nothing to gain.
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 100_000], [(105_000, ("a.py",))])
            result = compaction.assess(session)
        assert result is not None
        self.assertFalse(result.recommend)
        self.assertEqual(result.dead_tokens, 40_000)
        self.assertIn("fresh context", result.reason)

    def test_nothing_since_the_commit_is_not_a_moment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [])
            result = compaction.assess(session)
        assert result is not None
        self.assertFalse(result.recommend)
        self.assertIn("Nothing has happened", result.reason)

    def test_a_shed_since_the_commit_means_it_was_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ()), (70_000, ())])
            result = compaction.assess(session)
        assert result is not None
        self.assertFalse(result.recommend)
        self.assertIn("already shed", result.reason)

    # --- the lifecycle: each step on a line the tool wrote ------------------

    def _marker(self, kind: str, when: datetime, **extra: object) -> str:
        return json.dumps({"type": kind, "timestamp": when.isoformat().replace("+00:00", "Z"), **extra})

    def _append(self, session: LocalSession, lines: list[str]) -> None:
        with open(session.source_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_a_recommendation_is_the_nudge_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ("a.py",))])
            result = compaction.assess(session)
        assert result is not None
        self.assertTrue(result.recommend)
        self.assertEqual(result.stage, "nudge")
        self.assertIsNone(result.title)

    def test_the_typed_command_is_compacting_and_the_boundary_is_compacted(self) -> None:
        typed_at = self.COMMIT_AT + timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ("a.py",))])
            self._append(session, [
                json.dumps({"type": "custom-title", "customTitle": "Context health calibration"}),
                self._marker("user", typed_at, message={"role": "user", "content": "<command-name>/compact</command-name>"}),
            ])
            typed = compaction.assess(session)
            self._append(session, [self._marker("system", typed_at + timedelta(minutes=1), subtype="compact_boundary")])
            written = compaction.assess(session)
        assert typed is not None and written is not None
        self.assertEqual(typed.stage, "compacting")
        self.assertEqual(typed.title, "Context health calibration")
        self.assertEqual(typed.command_seen_at, typed_at.isoformat())
        self.assertIn("/compact was typed at", typed.reason)
        # Still recommended on the numbers -- nothing has shed -- but the
        # stage says the command is already in flight.
        self.assertTrue(typed.recommend)
        self.assertEqual(written.stage, "compacted")
        self.assertEqual(written.boundary_seen_at, (typed_at + timedelta(minutes=1)).isoformat())
        self.assertIn("the next reply will show the new size", written.reason)

    def test_the_first_small_reply_confirms_until_the_user_types_again(self) -> None:
        # Claude Code carries on by itself after a compaction -- on
        # 2026-09-09 the second reply came six seconds after the first -- so
        # the step that shows the real number ends on the user's next prompt,
        # not on the next reply.
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ()), (70_000, ())])
            confirmed = compaction.assess(session)
            self._append(session, [turn(when=self.COMMIT_AT + timedelta(minutes=9), context=75_000)])
            still = compaction.assess(session)
            self._append(session, [prompt(when=self.COMMIT_AT + timedelta(minutes=12))])
            later = compaction.assess(session)
        assert confirmed is not None and still is not None and later is not None
        self.assertEqual(confirmed.stage, "confirmed")
        self.assertEqual(confirmed.latest_turn_tokens, 70_000)
        self.assertEqual(confirmed.context_before_shed, 310_000)
        self.assertEqual(confirmed.context_after_shed, 70_000)
        # Another reply grows the context but does not end the step, and the
        # number on show stays the shed's, not the latest reply's.
        self.assertEqual(still.stage, "confirmed")
        self.assertEqual(still.latest_turn_tokens, 75_000)
        self.assertEqual(still.context_after_shed, 70_000)
        self.assertEqual(later.stage, "none")
        self.assertFalse(later.recommend)

    def test_a_prompt_typed_before_the_first_reply_does_not_end_the_step(self) -> None:
        # In a live replay, the boundary landed first, the user typed while the
        # compact was still waiting, and the first reply after the shed came
        # later. That prompt was
        # typed at a bar reading "Compacted" with no sizes yet; the sizes
        # arrive with the reply, so the step they belong to ends on the first
        # prompt typed *after* that reply, not on one typed while waiting.
        typed_at = self.COMMIT_AT + timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ("a.py",))])
            self._append(session, [
                self._marker("system", typed_at + timedelta(minutes=1), subtype="compact_boundary"),
                prompt(when=typed_at + timedelta(minutes=1, seconds=24)),
            ])
            waiting = compaction.assess(session)
            self._append(session, [turn(when=typed_at + timedelta(minutes=1, seconds=34), context=70_000)])
            confirmed = compaction.assess(session)
            self._append(session, [prompt(when=typed_at + timedelta(minutes=3))])
            later = compaction.assess(session)
        assert waiting is not None and confirmed is not None and later is not None
        self.assertEqual(waiting.stage, "compacted")
        self.assertEqual(confirmed.stage, "confirmed")
        self.assertEqual(confirmed.context_after_shed, 70_000)
        self.assertEqual(later.stage, "none")

    def test_markers_from_before_the_commit_are_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ("a.py",))])
            lines = Path(session.source_path).read_text(encoding="utf-8")
            old = self._marker("system", self.COMMIT_AT - timedelta(hours=2), subtype="compact_boundary")
            Path(session.source_path).write_text(old + "\n" + lines, encoding="utf-8")
            result = compaction.assess(session)
        assert result is not None
        self.assertEqual(result.stage, "nudge")
        self.assertIsNone(result.boundary_seen_at)

    def test_the_stage_rule_orders_the_steps(self) -> None:
        t0 = self.COMMIT_AT
        t1 = t0 + timedelta(minutes=1)
        rule = compaction._stage
        self.assertEqual(rule(recommend=True, shed=False, prompts_after_shed=0, command_at=None, boundary_at=None), "nudge")
        self.assertEqual(rule(recommend=False, shed=False, prompts_after_shed=0, command_at=None, boundary_at=None), "none")
        self.assertEqual(rule(recommend=True, shed=False, prompts_after_shed=0, command_at=t0, boundary_at=None), "compacting")
        self.assertEqual(rule(recommend=True, shed=False, prompts_after_shed=0, command_at=t0, boundary_at=t1), "compacted")
        # A new /compact typed after an earlier boundary is a new compaction.
        self.assertEqual(rule(recommend=True, shed=False, prompts_after_shed=0, command_at=t1, boundary_at=t0), "compacting")
        # The shed holds the last step until the user types again.
        self.assertEqual(rule(recommend=False, shed=True, prompts_after_shed=0, command_at=t0, boundary_at=t1), "confirmed")
        self.assertEqual(rule(recommend=False, shed=True, prompts_after_shed=1, command_at=t0, boundary_at=t1), "none")

    def test_a_session_that_started_after_the_commit_has_nothing_to_shed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [], [(60_000, ()), (300_000, ())])
            result = compaction.assess(session)
        assert result is not None
        self.assertFalse(result.recommend)
        self.assertEqual(result.context_at_commit, 0)

    def test_a_priced_model_carries_the_dollar_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ("a.py",))])
            result = compaction.assess(session)
        assert result is not None
        self.assertTrue(result.priced)
        assert result.dead_usd_per_turn is not None
        self.assertGreater(result.dead_usd_per_turn, 0.0)

    def test_no_transcript_or_no_repo_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._build(tmp, [60_000, 300_000], [(310_000, ())])
            self.assertIsNone(compaction.assess(LocalSession(session_id="x", tool="claude-code", project_path=tmp)))
            session.source_path = str(Path(tmp, "missing.jsonl"))
            self.assertIsNone(compaction.assess(session))


class CodexRolloutTests(unittest.TestCase):
    """The same verdict from a Codex rollout: per-call context from token_count
    events, prompts from the user rows, files from apply_patch."""

    COMMIT_AT = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)

    @staticmethod
    def _row(when: datetime, row_type: str, payload: dict) -> str:
        return json.dumps({"timestamp": when.isoformat().replace("+00:00", "Z"), "type": row_type, "payload": payload})

    def _token_count(self, when: datetime, context: int, total: int) -> str:
        return self._row(when, "event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total, "output_tokens": 100, "total_tokens": total + 100},
            "last_token_usage": {"input_tokens": context, "cached_input_tokens": context - 5_000, "output_tokens": 50},
        }})

    def _rollout(self, tmp: str, *, shed: bool = False) -> LocalSession:
        repo = Path(tmp)
        write_reflog(repo, [reflog_line(ZERO, SHA, self.COMMIT_AT, "commit: fix: thing")])
        t = self.COMMIT_AT
        lines = [
            self._row(t - timedelta(minutes=40), "session_meta", {"id": "codex-1", "cwd": str(repo), "originator": "codex-tui"}),
            self._row(t - timedelta(minutes=39), "turn_context", {"model": "gpt-5-codex", "cwd": str(repo)}),
            self._row(t - timedelta(minutes=38), "response_item", {"role": "user", "content": [{"type": "input_text", "text": "start"}]}),
            self._token_count(t - timedelta(minutes=37), 60_000, 60_000),
            self._token_count(t - timedelta(minutes=5), 300_000, 360_000),
            # The same total reported twice is one call, as in the scanner.
            self._token_count(t - timedelta(minutes=4), 300_000, 360_000),
            self._row(t + timedelta(minutes=1), "event_msg", {"type": "user_message", "message": "next"}),
            self._row(t + timedelta(minutes=2), "response_item", {
                "type": "custom_tool_call", "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch\n",
            }),
            self._token_count(t + timedelta(minutes=3), 70_000 if shed else 310_000, 670_000),
        ]
        rollout = repo / "rollout.jsonl"
        rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return LocalSession(
            session_id="codex-1", tool="codex-cli", project_path=str(repo), source_path=str(rollout),
            model="gpt-5-codex", started_at=t - timedelta(hours=1), updated_at=t + timedelta(minutes=5),
        )

    def test_a_codex_session_gets_the_same_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = compaction.assess(self._rollout(tmp))
        assert result is not None
        self.assertTrue(result.recommend)
        self.assertEqual(result.first_turn_tokens, 60_000)
        self.assertEqual(result.context_at_commit, 300_000)
        self.assertEqual(result.latest_turn_tokens, 310_000)
        self.assertEqual(result.dead_tokens, 240_000)
        self.assertEqual(result.after_estimate, 70_000)
        self.assertEqual(result.turns_since_commit, 1)
        self.assertEqual(result.prompts_since_commit, 1)
        self.assertEqual(result.files_since, ["src/a.py"])
        self.assertEqual(result.command, "/compact")
        self.assertFalse(result.priced)

    def test_a_codex_shed_since_the_commit_is_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = compaction.assess(self._rollout(tmp, shed=True))
        assert result is not None
        self.assertFalse(result.recommend)
        self.assertIn("already shed", result.reason)
        # Codex writes no marker for a compaction in progress, so the only
        # lifecycle step it can reach is the drop itself, and it is read the
        # same way: the small call is the latest, from 300k before it.
        self.assertEqual(result.stage, "confirmed")
        self.assertEqual(result.context_before_shed, 300_000)
        self.assertEqual(result.context_after_shed, 70_000)
        self.assertIsNone(result.command_seen_at)
        self.assertIsNone(result.title)

    def test_a_codex_prompt_after_the_shed_ends_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._rollout(tmp, shed=True)
            with open(session.source_path, "a", encoding="utf-8") as handle:
                handle.write(self._row(self.COMMIT_AT + timedelta(minutes=4), "event_msg", {"type": "user_message", "message": "carry on"}) + "\n")
            result = compaction.assess(session)
        assert result is not None
        self.assertEqual(result.stage, "none")


class CommandTests(unittest.TestCase):
    def test_codex_gets_the_bare_command(self) -> None:
        boundary = compaction.Boundary(sha=SHA, subject="s", committed_at=datetime.now(timezone.utc))
        self.assertEqual(compaction.compact_command("codex-cli", boundary, ["a.py"]), "/compact")

    def test_many_files_are_counted_not_listed(self) -> None:
        boundary = compaction.Boundary(sha=SHA, subject="s", committed_at=datetime.now(timezone.utc))
        command = compaction.compact_command("claude-code", boundary, [f"f{i}.py" for i in range(7)])
        self.assertIn("f0.py, f1.py, f2.py, f3.py and 3 more files", command)

    def test_no_files_still_names_the_boundary(self) -> None:
        boundary = compaction.Boundary(sha=SHA, subject="", committed_at=datetime.now(timezone.utc))
        command = compaction.compact_command("claude-code", boundary, [])
        self.assertIn(f"since commit {SHA[:7]}: the work since then", command)


if __name__ == "__main__":
    unittest.main()
