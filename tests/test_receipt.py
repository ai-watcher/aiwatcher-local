from __future__ import annotations

# Import the tests package for its side effect: it points AIWATCHER_HOME at a
# throwaway directory. Without it, single-file discovery can write receipt
# evidence into the developer's real ~/.aiwatcher ledger.
import tests  # noqa: F401

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.receipt import (
    MIN_LINES_FOR_RATE,
    build_commit_receipt,
    compute_repo_baselines,
    format_receipt,
    rewrite_in_progress,
    survival_note,
)
from aiwatcher_cli.scanner import LocalEvent


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def init_repo(path: str) -> None:
    run(["git", "init", "-q", "."], path)
    run(["git", "config", "user.email", "dev@example.com"], path)
    run(["git", "config", "user.name", "Dev"], path)


def commit(repo: str, name: str, body: str, subject: str, *, when: datetime) -> str:
    Path(repo, name).write_text(body, encoding="utf-8")
    run(["git", "-C", repo, "add", name], repo)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run(["git", "-C", repo, "commit", "-m", subject], repo, env=env)
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip()


def event(repo: str, *, cost: float, when: datetime, index: int = 0) -> LocalEvent:
    return LocalEvent(
        event_id=f"e{index}-{cost}",
        session_id="s1",
        tool="claude-code",
        event_type="model_usage",
        timestamp=when,
        project_path=repo,
        model="claude-sonnet-5",
        cost_usd=cost,
    )


def lines(count: int) -> str:
    return "".join(f"line {index}\n" for index in range(count))


class CommitReceiptTests(unittest.TestCase):
    def test_reports_cost_and_rate_for_the_commit(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", lines(20), "the change", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(
                repo, sha, [event(repo, cost=4.0, when=now - timedelta(hours=2))], now=now
            )

        self.assertTrue(receipt["available"])
        self.assertAlmostEqual(receipt["cost_usd"], 4.0, places=6)
        self.assertEqual(receipt["lines_added"], 20)
        self.assertAlmostEqual(receipt["usd_per_line"], 0.2, places=6)

    def test_unknown_commit_reports_unavailable_not_an_empty_receipt(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", lines(20), "the change", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(repo, "deadbeefdead", [], now=now)

        self.assertFalse(receipt["available"])
        self.assertIn("deadbeefdead", receipt["reason"])
        self.assertEqual(format_receipt(receipt), "")

    def test_tiny_change_gets_no_per_line_rate(self) -> None:
        # A one-line fix has a spectacular per-line cost and means nothing.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", "one\n", "typo", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(
                repo, sha, [event(repo, cost=2.0, when=now - timedelta(hours=2))], now=now
            )

        self.assertIsNone(receipt["usd_per_line"])
        self.assertIsNone(receipt["rate_ratio"])
        self.assertIn("too few lines", format_receipt(receipt))

    def test_repo_with_no_observed_spend_produces_no_receipt(self) -> None:
        # The hook stays silent rather than printing a receipt full of zeroes
        # for a repo AIWatcher has never seen AI work in.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", lines(20), "hand written", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(repo, sha, [], now=now)

        self.assertFalse(receipt["available"])
        self.assertEqual(format_receipt(receipt), "")

    def test_unattributed_commit_says_so_rather_than_zero(self) -> None:
        # The repo has spend, just none this commit can claim. "$0.00" would
        # read as "this was free" instead of "we saw nothing for it".
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", lines(20), "earlier", when=now - timedelta(hours=20))
            sha = commit(repo, "b.py", lines(20), "hand written", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(
                repo, sha,
                [event(repo, cost=3.0, when=now - timedelta(hours=21))],
                now=now,
            )

        self.assertTrue(receipt["available"])
        self.assertAlmostEqual(receipt["cost_usd"], 0.0, places=6)
        # No rate at all, rather than $0.00/line placing it below every median.
        self.assertIsNone(receipt["usd_per_line"])
        self.assertIsNone(receipt["rate_ratio"])
        self.assertIn("no AI spend observed", format_receipt(receipt))


class OutputEncodingTests(unittest.TestCase):
    def test_receipt_is_ascii_so_a_git_hook_cannot_mangle_it(self) -> None:
        # Caught live: the hook printed "$8.29 ? +195/-18" because a middle dot
        # and an em dash do not survive a cp1252 stdout, which is exactly the
        # console this runs in on Windows.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", lines(40), "a change", when=now - timedelta(hours=1))
            from aiwatcher_cli.ledger import repo_identity
            receipt = build_commit_receipt(
                repo, sha, [event(repo, cost=4.0, when=now - timedelta(hours=2))],
                now=now,
                baselines={repo_identity(repo): {
                    "median_usd_per_line": 0.5, "median_cost_usd": 6.0,
                    "changes": 9, "window_days": 30,
                }},
            )
            text = format_receipt(receipt, note="Earlier work: 72% still standing.")

        self.assertTrue(text)
        self.assertIn("cheaper than usual", text)
        text.encode("cp1252")  # must not raise
        self.assertTrue(text.isascii(), text)


class BaselineComparisonTests(unittest.TestCase):
    """Every figure names a comparison; a dollar amount with no baseline does
    not tell you whether to care."""

    def test_cached_baseline_drives_the_comparison(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", lines(100), "cheap change", when=now - timedelta(hours=1))
            from aiwatcher_cli.ledger import repo_identity
            baselines = {
                repo_identity(repo): {
                    "median_usd_per_line": 0.20,
                    "median_cost_usd": 8.0,
                    "changes": 12,
                    "window_days": 30,
                }
            }
            receipt = build_commit_receipt(
                repo, sha, [event(repo, cost=5.0, when=now - timedelta(hours=2))],
                now=now, baselines=baselines,
            )

        self.assertTrue(receipt["baseline_cached"])
        self.assertEqual(receipt["baseline_changes"], 12)
        # $0.05/line against a $0.20 median.
        self.assertAlmostEqual(receipt["rate_ratio"], 0.25, places=2)
        self.assertIn("cheaper than usual", format_receipt(receipt))

    def test_missing_baseline_says_so_instead_of_inventing_one(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, "a.py", lines(20), "a change", when=now - timedelta(hours=1))
            receipt = build_commit_receipt(
                repo, sha, [event(repo, cost=4.0, when=now - timedelta(hours=2))],
                now=now, baselines={},
            )

        self.assertFalse(receipt["baseline_cached"])
        self.assertIsNone(receipt["rate_ratio"])
        self.assertIn("not computed yet", format_receipt(receipt))

    def test_baselines_are_keyed_by_repo_identity(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", lines(40), "one", when=now - timedelta(days=3))
            commit(repo, "b.py", lines(40), "two", when=now - timedelta(days=2))
            events = [
                event(repo, cost=4.0, when=now - timedelta(days=3, hours=1), index=1),
                event(repo, cost=8.0, when=now - timedelta(days=2, hours=1), index=2),
            ]
            baselines = compute_repo_baselines(events, now=now)
            from aiwatcher_cli.ledger import repo_identity
            identity = repo_identity(repo)

        self.assertIn(identity, baselines)
        self.assertEqual(baselines[identity]["changes"], 2)
        self.assertIsNotNone(baselines[identity]["median_usd_per_line"])

    def test_tiny_changes_are_kept_out_of_the_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "typo", when=now - timedelta(days=3))
            commit(repo, "b.py", lines(50), "real work", when=now - timedelta(days=2))
            events = [
                event(repo, cost=9.0, when=now - timedelta(days=3, hours=1), index=1),
                event(repo, cost=5.0, when=now - timedelta(days=2, hours=1), index=2),
            ]
            baselines = compute_repo_baselines(events, now=now)
            from aiwatcher_cli.ledger import repo_identity
            entry = baselines[repo_identity(repo)]

        # The $9 one-line commit would otherwise set a $9/line median.
        self.assertEqual(entry["changes"], 1)
        self.assertAlmostEqual(entry["median_usd_per_line"], 0.1, places=6)


class RewriteSuppressionTests(unittest.TestCase):
    """post-commit fires once per commit a rebase replays. Ten receipts for
    work already reported when it was written is how a hook gets uninstalled.
    """

    def test_quiet_while_a_rebase_is_in_progress(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", lines(5), "one", when=now - timedelta(hours=3))
            self.assertFalse(rewrite_in_progress(repo))
            os.makedirs(Path(repo, ".git", "rebase-merge"), exist_ok=True)
            self.assertTrue(rewrite_in_progress(repo))

    def test_quiet_during_a_cherry_pick(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", lines(5), "one", when=now - timedelta(hours=3))
            Path(repo, ".git", "CHERRY_PICK_HEAD").write_text("x", encoding="utf-8")
            self.assertTrue(rewrite_in_progress(repo))

    def test_a_path_that_is_not_a_repo_is_not_treated_as_mid_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            self.assertFalse(rewrite_in_progress(plain))


class CommitHookInstallTests(unittest.TestCase):
    """The hook outlives any single checkout: installed once, it fires on every
    branch, including ones whose aiwatcher predates `commit-receipt`."""

    def _install(self, repo: str, **kw):
        from argparse import Namespace
        from aiwatcher_cli import cli
        args = Namespace(repo=repo, command="aiwatcher", write=True, **kw)
        return cli.command_install_commit_hook(args)

    def _hook(self, repo: str) -> str:
        return Path(repo, ".git", "hooks", "post-commit").read_text(encoding="utf-8")

    def test_hook_silences_stderr(self) -> None:
        # An older aiwatcher on another branch has argparse dump a usage error
        # before our code runs, so --quiet-if-empty never gets a say.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "base", when=now - timedelta(hours=1))
            self._install(repo)
            body = self._hook(repo)

        self.assertIn("2>/dev/null", body)
        self.assertIn("|| true", body)

    def test_reinstall_upgrades_an_outdated_hook_in_place(self) -> None:
        # Without this, a fix to the hook line only reaches people who have
        # never installed it.
        from aiwatcher_cli import cli
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "base", when=now - timedelta(hours=1))
            path = Path(repo, ".git", "hooks", "post-commit")
            path.parent.mkdir(parents=True, exist_ok=True)
            stale = (
                "#!/bin/sh\n\n"
                f"{cli.COMMIT_HOOK_MARKER}\n"
                "aiwatcher commit-receipt --quiet-if-empty || true\n"
                f"{cli.COMMIT_HOOK_END}\n"
            )
            path.write_text(stale, encoding="utf-8")
            self._install(repo)
            body = self._hook(repo)

        self.assertIn("2>/dev/null", body)
        self.assertEqual(body.count(cli.COMMIT_HOOK_MARKER), 1)

    def test_reinstalling_a_current_hook_changes_nothing(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "base", when=now - timedelta(hours=1))
            self._install(repo)
            first = self._hook(repo)
            self._install(repo)
            second = self._hook(repo)

        self.assertEqual(first, second)

    def test_upgrade_preserves_someone_elses_hook(self) -> None:
        from aiwatcher_cli import cli
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "base", when=now - timedelta(hours=1))
            path = Path(repo, ".git", "hooks", "post-commit")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "#!/bin/sh\necho theirs\n\n"
                f"{cli.COMMIT_HOOK_MARKER}\n"
                "aiwatcher commit-receipt --quiet-if-empty || true\n"
                f"{cli.COMMIT_HOOK_END}\n"
                "echo also-theirs\n",
                encoding="utf-8",
            )
            self._install(repo)
            body = self._hook(repo)

        self.assertIn("echo theirs", body)
        self.assertIn("echo also-theirs", body)
        self.assertIn("2>/dev/null", body)


class SurvivalNoteTests(unittest.TestCase):
    def test_reports_earlier_work_not_the_commit_just_made(self) -> None:
        receipt = {"available": True}
        note = survival_note(receipt, {"available": True, "survival_pct": 72.0, "changes_measured": 28})
        assert note is not None
        self.assertIn("Earlier work", note)
        self.assertIn("72%", note)
        # Never claims anything about the commit that just landed.
        self.assertNotIn("this change", note.lower())

    def test_silent_when_survival_has_not_been_measured(self) -> None:
        self.assertIsNone(survival_note({"available": True}, {}))
        self.assertIsNone(survival_note({"available": True}, None))
        self.assertIsNone(
            survival_note({"available": True}, {"available": False, "reason": "not enough history"})
        )

    def test_silent_when_the_receipt_itself_is_unavailable(self) -> None:
        self.assertIsNone(
            survival_note({"available": False}, {"available": True, "survival_pct": 90.0, "changes_measured": 5})
        )


if __name__ == "__main__":
    unittest.main()
