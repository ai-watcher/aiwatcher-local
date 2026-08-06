from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import patch

from aiwatcher_cli import ledger as ledger_module
from aiwatcher_cli.ledger import (
    UNBANKED_NO_COMMIT,
    UNBANKED_OUTSIDE_REPO,
    build_ledger,
    commits_since,
    cost_per_surviving_line,
    local_author_emails,
    repo_identity,
    unbanked_summary,
)
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

    def test_unreadable_repo_returns_none_not_empty(self) -> None:
        # None and [] must stay distinguishable: [] means the repo genuinely
        # committed nothing, which makes every dollar in it unbanked. A git
        # failure returning [] would fake that result and inflate the one
        # number the ledger exists to report.
        self.assertIsNone(commits_since("/no/such/repo/path", datetime.now(timezone.utc)))

    def test_repo_with_no_commits_in_window_returns_empty(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "old\n", "ancient", when=now - timedelta(days=90))
            self.assertEqual(commits_since(repo, now - timedelta(days=1)), [])


def repo_key(path: str) -> str:
    """The ledger keys repos by `git rev-parse --show-toplevel`, which returns
    forward slashes on Windows. Tests must compare against that, not the raw
    tempdir path."""
    return ledger_module._repo_root(path) or path


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


class UnbankedReasonTests(unittest.TestCase):
    def test_outside_repo_and_no_commit_are_counted_separately(self) -> None:
        # They call for different responses, so the card must not merge them.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as loose:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "earlier", when=now - timedelta(hours=5))
            led = build_ledger([
                event(repo, cost=7.0, when=now - timedelta(hours=1)),
                event(loose, cost=2.0, when=now - timedelta(hours=1)),
            ], days=7, now=now)

        self.assertAlmostEqual(led.unbanked_by_reason[UNBANKED_NO_COMMIT], 7.0, places=6)
        self.assertAlmostEqual(led.unbanked_by_reason[UNBANKED_OUTSIDE_REPO], 2.0, places=6)
        self.assertAlmostEqual(led.unbanked_usd, 9.0, places=6)

    def test_unbanked_is_attributed_to_the_repo_it_happened_in(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "earlier", when=now - timedelta(hours=5))
            led = build_ledger([event(repo, cost=4.0, when=now - timedelta(hours=1))],
                               days=7, now=now)
            key = repo_key(repo)

        self.assertAlmostEqual(led.unbanked_by_repo[key], 4.0, places=6)


class UnresolvedSpendTests(unittest.TestCase):
    """A git failure must not masquerade as waste.

    build_ledger treats a repo with no commits as fully unbanked. If a failed
    `git log` also returned "no commits", every dollar in an unreadable repo
    would be reported as money with nothing to show for it -- inflating the
    headline silently, in the one direction that makes the product look most
    alarming.
    """

    def test_unreadable_repo_is_unresolved_not_unbanked(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            events = [event(repo, cost=6.0, when=now - timedelta(hours=1))]
            with patch.object(ledger_module, "commits_since", return_value=None):
                led = build_ledger(events, days=7, now=now)
            key = repo_key(repo)

        self.assertAlmostEqual(led.unresolved_usd, 6.0, places=6)
        self.assertEqual(led.unresolved_events, 1)
        self.assertEqual(led.unresolved_repos, [key])
        self.assertAlmostEqual(led.unbanked_usd, 0.0, places=6)

    def test_unresolved_spend_is_excluded_from_the_unbanked_rate(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as good, tempfile.TemporaryDirectory() as bad:
            init_repo(good)
            init_repo(bad)
            commit(good, "a.py", "x\n", "landed", when=now - timedelta(hours=1))
            real = ledger_module.commits_since
            bad_key = repo_key(bad)

            def flaky(repo: str, since: datetime):
                return None if repo == bad_key else real(repo, since)

            events = [
                event(good, cost=3.0, when=now - timedelta(hours=2)),
                event(bad, cost=97.0, when=now - timedelta(hours=2)),
            ]
            with patch.object(ledger_module, "commits_since", side_effect=flaky):
                led = build_ledger(events, days=7, now=now)

        # $97 is unknown, not wasted: the rate is 0% of the $3 we could classify.
        self.assertAlmostEqual(led.unresolved_usd, 97.0, places=6)
        self.assertAlmostEqual(led.classified_usd, 3.0, places=6)
        self.assertAlmostEqual(led.unbanked_pct, 0.0, places=6)
        self.assertAlmostEqual(led.total_usd, 100.0, places=6)


class PerChangeFieldTests(unittest.TestCase):
    """The per-change view needs cost and $/line per commit, plus survival
    joined in from the cached summary. These pin the fields it reads.
    """

    def test_usd_per_line_divides_cost_by_lines_touched(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "1\n2\n3\n4\n", "four lines", when=now - timedelta(hours=1))
            led = build_ledger([event(repo, cost=8.0, when=now - timedelta(hours=2))],
                               days=7, now=now)

        change = led.changes[0]
        self.assertEqual(change.lines_changed, 4)
        self.assertAlmostEqual(change.usd_per_line, 2.0, places=6)

    def test_commit_with_no_observed_spend_has_no_rate(self) -> None:
        # None, not 0.0: nobody spent $0/line, we just did not see the spend.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "hand written", when=now - timedelta(hours=1))
            led = build_ledger([], days=7, now=now)

        self.assertEqual(led.changes, [])

    def test_zero_line_commit_has_no_rate_instead_of_dividing_by_zero(self) -> None:
        change = ledger_module.Change(
            sha="abc", repo="/r", subject="empty",
            committed_at=datetime.now(timezone.utc), cost_usd=5.0,
        )
        self.assertIsNone(change.usd_per_line)

    def test_survival_records_per_change_results(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            for index in range(3):
                commit(repo, f"f{index}.py", "a\nb\n", f"change {index}",
                       when=now - timedelta(days=20, hours=index))
            events = [
                event(repo, cost=3.0, when=now - timedelta(days=20, hours=index, minutes=30))
                for index in range(3)
            ]
            led = build_ledger(events, days=60, now=now)
            result = cost_per_surviving_line(led, now=now)

        by_change = result.get("by_change") or {}
        self.assertTrue(by_change, "per-change survival must be recorded, not discarded")
        for entry in by_change.values():
            self.assertIn("measurable", entry)
            if entry["measurable"]:
                self.assertIn("survival_pct", entry)


class GitOutputEncodingTests(unittest.TestCase):
    def test_non_ascii_commit_subject_survives_decoding(self) -> None:
        # text=True without an explicit encoding decodes with the locale codec,
        # which is cp1252 on Windows. git emits UTF-8, so an em dash came back
        # as "a€"" in the change table.
        now = datetime.now(timezone.utc)
        subject = "feat: surface unbanked spend — money with no commit"
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", subject, when=now - timedelta(hours=1))
            changes = commits_since(repo, now - timedelta(days=1))

        self.assertEqual(changes[0].subject, subject)


def commit_as(repo: str, path: str, body: str, subject: str, *, when, name: str, email: str) -> None:
    """Commit under a specific author identity, to stand in for work that
    arrived by fetch from another developer's machine."""
    Path(repo, path).write_text(body, encoding="utf-8")
    run(["git", "-C", repo, "add", path], repo)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    run(["git", "-C", repo, "commit", "-m", subject], repo, env=env)


class CloneDeduplicationTests(unittest.TestCase):
    """Two checkouts of one repository are one repository.

    Repos were keyed by filesystem path, so a second clone counted every commit
    a second time -- 93 phantom changes out of 259 locally, all showing "no
    spend observed" because the first copy had already claimed the money.
    """

    def test_clones_share_a_repo_identity(self) -> None:
        with tempfile.TemporaryDirectory() as origin, tempfile.TemporaryDirectory() as parent:
            init_repo(origin)
            commit(origin, "a.py", "x\n", "root", when=datetime.now(timezone.utc) - timedelta(hours=2))
            clone = str(Path(parent, "copy"))
            run(["git", "clone", "-q", origin, clone], parent)
            self.assertEqual(repo_identity(origin), repo_identity(clone))

    def test_unrelated_repos_do_not_share_an_identity(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            init_repo(one)
            init_repo(two)
            commit(one, "a.py", "x\n", "one", when=now - timedelta(hours=2))
            commit(two, "a.py", "x\n", "two", when=now - timedelta(hours=2))
            self.assertNotEqual(repo_identity(one), repo_identity(two))

    def test_a_commit_present_in_two_clones_appears_once(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as origin, tempfile.TemporaryDirectory() as parent:
            init_repo(origin)
            commit(origin, "a.py", "x\n", "shared work", when=now - timedelta(hours=2))
            clone = str(Path(parent, "copy"))
            run(["git", "clone", "-q", origin, clone], parent)
            led = build_ledger([
                event(origin, cost=4.0, when=now - timedelta(hours=3)),
                event(clone, cost=1.0, when=now - timedelta(hours=3)),
            ], days=7, now=now)

        self.assertEqual(len(led.changes), 1, "the same commit must not appear once per clone")
        # Spend from both checkouts lands on the one commit they share.
        self.assertAlmostEqual(led.changes[0].cost_usd, 5.0, places=6)


class ForeignCommitTests(unittest.TestCase):
    """Commits that arrived by fetch were made on another machine.

    Counting them made 53 of 166 unattributed commits locally look like missing
    coverage. Worse, the attribution rule (spend before a commit belongs to it)
    let a teammate's fetched commit absorb the local session that preceded it --
    10 commits were showing another developer's dollars.
    """

    def test_someone_elses_commit_is_excluded(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "mine.py", "x\n", "my work", when=now - timedelta(hours=2))
            commit_as(repo, "theirs.py", "y\n", "their work", when=now - timedelta(hours=1),
                      name="Someone Else", email="someone@example.com")
            led = build_ledger([event(repo, cost=5.0, when=now - timedelta(hours=3))],
                               days=7, now=now)

        self.assertEqual([c.subject for c in led.changes], ["my work"])
        self.assertEqual(led.foreign_changes, 1)

    def test_a_fetched_commit_does_not_absorb_local_spend(self) -> None:
        # The teammate's commit lands between the local session and the local
        # commit. Before the filter it took the money.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit_as(repo, "theirs.py", "y\n", "their work", when=now - timedelta(hours=3),
                      name="Someone Else", email="someone@example.com")
            commit(repo, "mine.py", "x\n", "my work", when=now - timedelta(hours=2))
            led = build_ledger([event(repo, cost=9.0, when=now - timedelta(hours=4))],
                               days=7, now=now)

        self.assertEqual(len(led.changes), 1)
        self.assertEqual(led.changes[0].subject, "my work")
        self.assertAlmostEqual(led.changes[0].cost_usd, 9.0, places=6)

    def test_no_configured_identity_counts_every_commit(self) -> None:
        # Dropping every commit because git has no user.email would be worse
        # than the problem being fixed.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit_as(repo, "a.py", "y\n", "somebody", when=now - timedelta(hours=1),
                      name="Someone Else", email="someone@example.com")
            with patch.object(ledger_module, "local_author_emails", return_value=set()):
                led = build_ledger([event(repo, cost=3.0, when=now - timedelta(hours=2))],
                                   days=7, now=now)

        self.assertEqual(len(led.changes), 1)
        self.assertEqual(led.foreign_changes, 0)


def commit_rewritten(repo: str, path: str, body: str, subject: str, *, authored, committed) -> None:
    """Commit with an author date behind its committer date — what a rebase,
    cherry-pick, or late amend leaves behind."""
    Path(repo, path).write_text(body, encoding="utf-8")
    run(["git", "-C", repo, "add", path], repo)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": authored.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "GIT_COMMITTER_DATE": committed.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    run(["git", "-C", repo, "commit", "-m", subject], repo, env=env)


class RebaseAttributionTests(unittest.TestCase):
    """`git rebase` restamps the committer date to the moment of the rebase.

    Keying attribution off that pushed the original work outside the 12h
    lookback: 30 of 32 rewritten commits locally read as free while the money
    behind them read as waste. Both records were correct and simply could not
    find each other. Attribution keys off the author date instead.
    """

    def test_rebased_commit_keeps_the_spend_that_produced_it(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            # Worked and committed 5 days ago, rebased 10 minutes ago.
            commit_rewritten(repo, "a.py", "x\n", "the work",
                             authored=now - timedelta(days=5),
                             committed=now - timedelta(minutes=10))
            led = build_ledger([event(repo, cost=6.0, when=now - timedelta(days=5, hours=1))],
                               days=30, now=now)

        self.assertEqual(len(led.changes), 1)
        self.assertTrue(led.changes[0].was_rewritten)
        self.assertAlmostEqual(led.changes[0].cost_usd, 6.0, places=6)
        self.assertAlmostEqual(led.unbanked_usd, 0.0, places=6)

    def test_rebasing_does_not_drag_old_work_into_a_recent_window(self) -> None:
        # The mirror risk: a commit authored 40 days ago but rebased today must
        # not enter a 7-day window, or it would appear with no spend beside it.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit_rewritten(repo, "a.py", "x\n", "ancient work",
                             authored=now - timedelta(days=40),
                             committed=now - timedelta(minutes=5))
            led = build_ledger([event(repo, cost=2.0, when=now - timedelta(hours=1))],
                               days=7, now=now)

        self.assertEqual(led.changes, [])
        self.assertAlmostEqual(led.unbanked_usd, 2.0, places=6)

    def test_an_unrewritten_commit_is_unaffected(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "normal", when=now - timedelta(hours=1))
            led = build_ledger([event(repo, cost=4.0, when=now - timedelta(hours=2))],
                               days=7, now=now)

        self.assertFalse(led.changes[0].was_rewritten)
        self.assertAlmostEqual(led.changes[0].cost_usd, 4.0, places=6)

    def test_author_date_in_the_future_falls_back_to_the_committer_date(self) -> None:
        # An explicit --date or a skewed clock must not put a commit's
        # attribution window ahead of the commit itself.
        now = datetime.now(timezone.utc)
        change = ledger_module.Change(
            sha="abc", repo="/r", subject="skewed",
            committed_at=now - timedelta(hours=1),
            authored_at=now + timedelta(days=3),
        )
        self.assertEqual(change.landed_at, now - timedelta(hours=1))
        self.assertFalse(change.was_rewritten)

    def test_commits_order_by_when_the_work_happened(self) -> None:
        # A rebase can restamp several commits to the same second. Ordering by
        # committer date would make that order arbitrary; author dates keep the
        # sequence the work was actually done in.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            stamp = now - timedelta(minutes=5)
            commit_rewritten(repo, "a.py", "1\n", "first work",
                             authored=now - timedelta(days=3), committed=stamp)
            commit_rewritten(repo, "b.py", "2\n", "second work",
                             authored=now - timedelta(days=2), committed=stamp)
            changes = commits_since(repo, now - timedelta(days=30))

        self.assertEqual([c.subject for c in changes], ["first work", "second work"])

    def test_spend_banks_to_the_sibling_it_preceded_after_a_rebase(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            stamp = now - timedelta(minutes=5)
            commit_rewritten(repo, "a.py", "1\n", "day three",
                             authored=now - timedelta(days=3), committed=stamp)
            commit_rewritten(repo, "b.py", "2\n", "day two",
                             authored=now - timedelta(days=2), committed=stamp)
            led = build_ledger([
                event(repo, cost=3.0, when=now - timedelta(days=3, hours=1)),
                event(repo, cost=7.0, when=now - timedelta(days=2, hours=1)),
            ], days=30, now=now)

        by_subject = {c.subject: c.cost_usd for c in led.changes}
        self.assertAlmostEqual(by_subject["day three"], 3.0, places=6)
        self.assertAlmostEqual(by_subject["day two"], 7.0, places=6)


class UnbankedSummaryTests(unittest.TestCase):
    def test_reports_share_repos_and_reasons(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "landed", when=now - timedelta(hours=3))
            led = build_ledger([
                event(repo, cost=10.0, when=now - timedelta(hours=4)),
                event(repo, cost=30.0, when=now - timedelta(minutes=30)),
            ], days=7, now=now)
            key = repo_key(repo)
        card = unbanked_summary(led)

        self.assertTrue(card["available"])
        self.assertAlmostEqual(card["unbanked_usd"], 30.0, places=6)
        self.assertAlmostEqual(card["banked_usd"], 10.0, places=6)
        self.assertEqual(card["unbanked_pct"], 75.0)
        self.assertEqual(card["top_repos"][0]["repo"], key)

    def test_trivial_unbanked_spend_reports_nothing(self) -> None:
        # A card claiming $0.40 of waste trains people to ignore the card.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "earlier", when=now - timedelta(hours=5))
            led = build_ledger([event(repo, cost=0.4, when=now - timedelta(hours=1))],
                               days=7, now=now)

        self.assertFalse(unbanked_summary(led)["available"])

    def test_no_spend_at_all_reports_nothing(self) -> None:
        self.assertFalse(unbanked_summary(build_ledger([], days=7))["available"])


if __name__ == "__main__":
    unittest.main()


class CostPerSurvivingLineTests(unittest.TestCase):
    """Replaces the survived/churned split that was built on git reachability.
    Survival is continuous here, so a change 60% still standing counts as 60%
    rather than as a coin flip against a threshold."""

    def _ledger(self, repo, events, *, now, days=30):
        return build_ledger(events, days=days, now=now)

    def test_unavailable_until_enough_changes_are_old_enough(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "recent", when=now - timedelta(hours=2))
            led = build_ledger([event(repo, cost=5.0, when=now - timedelta(hours=3))],
                               days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertFalse(result["available"])
        self.assertIn("old enough", result["reason"])

    def test_recent_changes_are_held_back_not_scored(self) -> None:
        # Including them would flatter the figure: nothing has had time to be
        # rewritten, so they would all score near 100%.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, "a.py", "x\n", "yesterday", when=now - timedelta(days=1))
            led = build_ledger([event(repo, cost=9.0, when=now - timedelta(days=1, hours=1))],
                               days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertEqual(result["changes_too_recent"], 1)
        self.assertEqual(result["changes_measured"], 0)

    def test_measures_aged_changes_and_reports_coverage(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            events = []
            for i in range(4):
                when = now - timedelta(days=20 - i)
                commit(repo, f"f{i}.py", "a\nb\nc\n", f"change {i}", when=when)
                events.append(event(repo, cost=10.0, when=when - timedelta(hours=1),
                                    session=f"s{i}"))
            led = build_ledger(events, days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertTrue(result["available"])
        self.assertGreaterEqual(result["changes_measured"], 3)
        self.assertEqual(result["survival_pct"], 100.0)
        self.assertIsNotNone(result["cost_coverage_pct"])

    def test_surviving_line_costs_more_than_a_touched_line_when_work_churns(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            events = []
            for i in range(4):
                when = now - timedelta(days=20 - i)
                commit(repo, f"f{i}.py", "a\nb\nc\nd\n", f"change {i}", when=when)
                events.append(event(repo, cost=10.0, when=when - timedelta(hours=1),
                                    session=f"s{i}"))
            # Rewrite half of one change's lines well after the fact.
            commit(repo, "f0.py", "a\nb\nZ\nY\n", "rewrite half of change 0",
                   when=now - timedelta(days=9))
            led = build_ledger(events, days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertLess(result["survival_pct"], 100.0)
        self.assertGreater(result["usd_per_surviving_line"], result["usd_per_line"])

    def test_free_changes_are_not_counted_as_measured_spend(self) -> None:
        # A commit made without AI has no cost to divide, so it must not dilute
        # the ratio or consume the coverage budget.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            events = []
            for i in range(3):
                when = now - timedelta(days=20 - i)
                commit(repo, f"paid{i}.py", "a\nb\n", f"paid {i}", when=when)
                events.append(event(repo, cost=10.0, when=when - timedelta(hours=1),
                                    session=f"s{i}"))
            commit(repo, "free.py", "x\n" * 50, "no AI involved", when=now - timedelta(days=15))
            led = build_ledger(events, days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertEqual(result["changes_considered"], 3)
        self.assertAlmostEqual(result["cost_usd"], 30.0, places=6)

    def test_reports_itself_as_a_floor(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            events = []
            for i in range(3):
                when = now - timedelta(days=20 - i)
                commit(repo, f"f{i}.py", "a\n", f"change {i}", when=when)
                events.append(event(repo, cost=1.0, when=when - timedelta(hours=1),
                                    session=f"s{i}"))
            led = build_ledger(events, days=30, now=now)
            result = cost_per_surviving_line(led, now=now)

        self.assertTrue(result["is_floor"])
