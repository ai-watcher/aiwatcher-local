from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.ledger import build_ledger, commits_since
from aiwatcher_cli.scanner import LocalEvent


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def init_repo(temp_dir: str) -> None:
    run(["git", "init"], temp_dir)
    run(["git", "config", "user.email", "test@example.com"], temp_dir)
    run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)


def commit(temp_dir: str, filename: str, content: str, message: str, *, when: datetime) -> None:
    (Path(temp_dir) / filename).write_text(content, encoding="utf-8")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run(["git", "add", "-A"], temp_dir, env=env)
    run(["git", "commit", "-m", message], temp_dir, env=env)


def event(repo: str, *, cost: float, when: datetime, session: str = "s1",
          model: str = "claude-sonnet-5", tool: str = "claude-code") -> LocalEvent:
    return LocalEvent(
        event_id=f"{session}-{when.isoformat()}",
        session_id=session,
        tool=tool,
        event_type="assistant",
        timestamp=when,
        project_path=repo,
        model=model,
        cost_usd=cost,
    )


class CommitsSinceTests(unittest.TestCase):
    def test_reads_line_counts_from_numstat(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "one\ntwo\nthree\n", "add three lines", when=now - timedelta(hours=2))
            changes = commits_since(repo, now - timedelta(days=1))

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].lines_added, 3)
        self.assertEqual(changes[0].files_changed, 1)
        self.assertEqual(changes[0].subject, "add three lines")

    def test_returns_oldest_first(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "1\n", "first", when=now - timedelta(hours=5))
            commit(repo, "b.py", "2\n", "second", when=now - timedelta(hours=3))
            changes = commits_since(repo, now - timedelta(days=1))

        self.assertEqual([c.subject for c in changes], ["first", "second"])

    def test_merge_commits_are_skipped(self) -> None:
        # A merge's diff is the union of work already counted on the commits it
        # merges; including it would double-count every line.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "base.py", "base\n", "base", when=now - timedelta(hours=6))
            run(["git", "checkout", "-q", "-b", "side"], repo)
            commit(repo, "side.py", "side\n", "side work", when=now - timedelta(hours=5))
            run(["git", "checkout", "-q", _default_branch(repo)], repo)
            commit(repo, "trunk.py", "trunk\n", "trunk work", when=now - timedelta(hours=4))
            stamp = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")
            env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            run(["git", "merge", "--no-ff", "-m", "merge side", "side"], repo, env=env)
            changes = commits_since(repo, now - timedelta(days=1))

        self.assertNotIn("merge side", [c.subject for c in changes])

    def test_unreadable_repo_returns_nothing(self) -> None:
        self.assertEqual(commits_since("/no/such/repo/path", datetime.now(timezone.utc)), [])


def _default_branch(repo: str) -> str:
    """`git init` names the first branch master or main depending on the git
    version and the user's config, so tests must ask rather than assume."""
    result = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"],
                            capture_output=True, text=True)
    return "master" if result.returncode == 0 else "main"


class LedgerAttributionTests(unittest.TestCase):
    def test_spend_banks_against_the_commit_that_followed_it(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "the change", when=now - timedelta(hours=1))
            events = [event(repo, cost=5.0, when=now - timedelta(hours=2))]
            led = build_ledger(events, days=7, now=now)

        self.assertEqual(len(led.changes), 1)
        self.assertAlmostEqual(led.changes[0].cost_usd, 5.0, places=6)
        self.assertAlmostEqual(led.banked_usd, 5.0, places=6)
        self.assertAlmostEqual(led.unbanked_usd, 0.0, places=6)

    def test_spend_after_the_last_commit_is_unbanked(self) -> None:
        # Work in progress, or exploration that never landed. It must not be
        # forced onto an earlier commit that it cannot have contributed to.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "earlier", when=now - timedelta(hours=5))
            events = [event(repo, cost=7.0, when=now - timedelta(hours=1))]
            led = build_ledger(events, days=7, now=now)

        self.assertAlmostEqual(led.unbanked_usd, 7.0, places=6)
        self.assertEqual(led.unbanked_events, 1)
        self.assertAlmostEqual(led.banked_usd, 0.0, places=6)

    def test_spend_older_than_the_lookback_cap_is_unbanked(self) -> None:
        # A trivial commit made after a week away must not inherit every dollar
        # spent before the gap.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "much later", when=now - timedelta(hours=1))
            events = [event(repo, cost=9.0, when=now - timedelta(hours=30))]
            led = build_ledger(events, days=7, now=now, max_lookback_hours=12.0)

        self.assertAlmostEqual(led.unbanked_usd, 9.0, places=6)
        self.assertAlmostEqual(led.banked_usd, 0.0, places=6)

    def test_a_long_session_splits_across_the_commits_it_spans(self) -> None:
        # The reason attribution is per-event: a session spanning two commits
        # should pay into both at the turns that actually happened, not dump
        # its whole cost on whichever commit came last.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "first", when=now - timedelta(hours=4))
            commit(repo, "b.py", "y\n", "second", when=now - timedelta(hours=2))
            events = [
                event(repo, cost=3.0, when=now - timedelta(hours=5), session="long"),
                event(repo, cost=8.0, when=now - timedelta(hours=3), session="long"),
            ]
            led = build_ledger(events, days=7, now=now)

        by_subject = {c.subject: c.cost_usd for c in led.changes}
        self.assertAlmostEqual(by_subject["first"], 3.0, places=6)
        self.assertAlmostEqual(by_subject["second"], 8.0, places=6)

    def test_parallel_sessions_both_count_toward_the_commit(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "shared", when=now - timedelta(hours=1))
            events = [
                event(repo, cost=4.0, when=now - timedelta(hours=2), session="a"),
                event(repo, cost=6.0, when=now - timedelta(hours=2), session="b"),
            ]
            led = build_ledger(events, days=7, now=now)

        self.assertAlmostEqual(led.changes[0].cost_usd, 10.0, places=6)
        self.assertEqual(led.changes[0].session_ids, ["a", "b"])

    def test_zero_cost_events_do_not_inflate_counts(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "the change", when=now - timedelta(hours=1))
            events = [
                event(repo, cost=2.0, when=now - timedelta(hours=2)),
                event(repo, cost=0.0, when=now - timedelta(hours=2)),
            ]
            led = build_ledger(events, days=7, now=now)

        self.assertEqual(led.changes[0].event_count, 1)

    def test_repo_without_commits_leaves_everything_unbanked(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            led = build_ledger([event(repo, cost=3.0, when=now - timedelta(hours=1))],
                               days=7, now=now)

        self.assertAlmostEqual(led.unbanked_usd, 3.0, places=6)
        self.assertEqual(led.changes, [])

    def test_spend_outside_any_repo_is_unbanked(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as not_a_repo:
            led = build_ledger([event(not_a_repo, cost=2.5, when=now - timedelta(hours=1))],
                               days=7, now=now)

        self.assertAlmostEqual(led.unbanked_usd, 2.5, places=6)

    def test_totals_always_reconcile(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "landed", when=now - timedelta(hours=3))
            events = [
                event(repo, cost=5.0, when=now - timedelta(hours=4)),
                event(repo, cost=11.0, when=now - timedelta(minutes=30)),
            ]
            led = build_ledger(events, days=7, now=now)

        self.assertAlmostEqual(led.total_usd, 16.0, places=6)
        self.assertAlmostEqual(led.banked_usd + led.unbanked_usd, led.total_usd, places=6)
        self.assertAlmostEqual(led.unbanked_pct, 100.0 * 11.0 / 16.0, places=1)

    def test_events_outside_the_window_are_ignored(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "recent", when=now - timedelta(hours=1))
            events = [event(repo, cost=99.0, when=now - timedelta(days=40))]
            led = build_ledger(events, days=7, now=now)

        self.assertAlmostEqual(led.total_usd, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
