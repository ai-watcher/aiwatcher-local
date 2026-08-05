from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.outcome_evidence import check_commit_survival
from aiwatcher_cli.survival import measure_change_survival


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def out(command: list[str], cwd: str) -> str:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def init_repo(temp_dir: str) -> None:
    run(["git", "init"], temp_dir)
    run(["git", "config", "user.email", "test@example.com"], temp_dir)
    run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)


def commit(temp_dir: str, files: dict[str, str | None], message: str) -> str:
    for name, content in files.items():
        path = Path(temp_dir) / name
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(content, encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run(["git", "add", "-A"], temp_dir, env=env)
    run(["git", "commit", "-m", message], temp_dir, env=env)
    return out(["git", "rev-parse", "HEAD"], temp_dir)


class LineSurvivalTests(unittest.TestCase):
    def test_untouched_work_survives_fully(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\nthree\n"}, "add three")
            commit(repo, {"b.py": "unrelated\n"}, "unrelated work")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertTrue(result.measurable)
        self.assertEqual(result.lines_added, 3)
        self.assertEqual(result.lines_surviving, 3)
        self.assertEqual(result.survival_pct, 100.0)

    def test_rewritten_lines_do_not_survive(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\nthree\n"}, "add three")
            commit(repo, {"a.py": "ONE\nTWO\nTHREE\n"}, "rewrite all three")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_surviving, 0)
        self.assertEqual(result.survival_pct, 0.0)

    def test_partial_rewrite_is_reported_as_a_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\nthree\nfour\n"}, "add four")
            commit(repo, {"a.py": "one\ntwo\nCHANGED\nALSO\n"}, "rewrite half")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_added, 4)
        self.assertEqual(result.lines_surviving, 2)
        self.assertEqual(result.survival_pct, 50.0)

    def test_reverted_work_reads_as_churn_where_reachability_says_survived(self) -> None:
        # The headline gap. `git revert` adds a new commit, so the original is
        # still reachable and the old check calls it a success.
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"base.py": "base\n"}, "base")
            sha = commit(repo, {"feature.py": "a\nb\nc\n"}, "add feature")
            run(["git", "revert", "--no-edit", sha], repo)
            result = measure_change_survival(repo, sha, min_age_days=0)

            self.assertEqual(check_commit_survival(repo, sha), "survived")

        self.assertEqual(result.lines_surviving, 0)
        self.assertEqual(result.survival_pct, 0.0)

    def test_deleted_file_takes_its_added_lines_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"keep.py": "keep\n"}, "base")
            sha = commit(repo, {"scratch.py": "x\ny\n"}, "add scratch")
            commit(repo, {"scratch.py": None}, "drop scratch")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_added, 2)
        self.assertEqual(result.lines_surviving, 0)
        self.assertEqual(result.survival_pct, 0.0)

    def test_a_cleanup_that_stuck_counts_as_survival(self) -> None:
        # The reason the measure is symmetric. A change whose whole job was
        # deleting dead code adds nothing, so an additions-only measure would
        # score it 0% -- exactly backwards.
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"a.py": "keep\ndead one\ndead two\ndead three\n"}, "base")
            sha = commit(repo, {"a.py": "keep\n"}, "delete dead code")
            commit(repo, {"b.py": "later\n"}, "unrelated later work")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_removed, 3)
        self.assertEqual(result.removals_holding, 3)
        self.assertEqual(result.survival_pct, 100.0)

    def test_a_cleanup_that_was_undone_counts_as_churn(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"a.py": "keep\ndead one\ndead two\n"}, "base")
            sha = commit(repo, {"a.py": "keep\n"}, "delete dead code")
            commit(repo, {"a.py": "keep\ndead one\ndead two\n"}, "put it back")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_removed, 2)
        self.assertEqual(result.removals_holding, 0)
        self.assertEqual(result.survival_pct, 0.0)

    def test_whitespace_only_reformatting_does_not_destroy_attribution(self) -> None:
        # blame -w is what stops a reformat from reading as total churn.
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\n"}, "add two")
            commit(repo, {"a.py": "    one\n    two\n"}, "reindent")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_surviving, 2)

    def test_empty_commit_is_not_measurable_rather_than_zero(self) -> None:
        # Reporting 0% for a change with nothing to measure would look like
        # total churn instead of "no signal".
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"a.py": "x\n"}, "base")
            run(["git", "commit", "--allow-empty", "-m", "empty"], repo)
            sha = out(["git", "rev-parse", "HEAD"], repo)
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertFalse(result.measurable)
        self.assertIsNone(result.survival_pct)
        self.assertIsNotNone(result.reason)

    def test_unreadable_repo_is_not_measurable(self) -> None:
        result = measure_change_survival("/no/such/repo/path", "abc123", min_age_days=0)
        self.assertFalse(result.measurable)
        self.assertIsNone(result.survival_pct)

    def test_mixed_add_and_delete_scores_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            commit(repo, {"a.py": "old one\nold two\n"}, "base")
            sha = commit(repo, {"a.py": "new one\nnew two\n"}, "swap contents")
            result = measure_change_survival(repo, sha, min_age_days=0)

        self.assertEqual(result.lines_added, 2)
        self.assertEqual(result.lines_removed, 2)
        self.assertEqual(result.lines_touched, 4)
        self.assertEqual(result.survival_pct, 100.0)


class SurvivalAgeGateTests(unittest.TestCase):
    """Survival decays steeply with age -- measured on this repo, 95% of lines
    from the last 3 days are still standing versus 67% at 14-45 days. Scoring a
    fresh commit reports how recent it is, not whether it stuck."""

    def test_a_fresh_change_is_not_yet_judgeable(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\n"}, "just landed")
            result = measure_change_survival(repo, sha)

        self.assertFalse(result.measurable)
        self.assertIsNone(result.survival_pct)
        self.assertIn("Too recent", result.reason)

    def test_an_aged_change_is_measured(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "one\ntwo\n"}, "landed a while ago")
            # Judge it from 30 days in the future rather than backdating git.
            future = datetime.now(timezone.utc) + timedelta(days=30)
            result = measure_change_survival(repo, sha, now=future)

        self.assertTrue(result.measurable)
        self.assertEqual(result.survival_pct, 100.0)
        self.assertGreater(result.age_days, 7)

    def test_age_is_reported_even_when_not_measurable(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo)
            sha = commit(repo, {"a.py": "x\n"}, "fresh")
            result = measure_change_survival(repo, sha)

        self.assertIsNotNone(result.age_days)
        self.assertLess(result.age_days, 1)


if __name__ == "__main__":
    unittest.main()
