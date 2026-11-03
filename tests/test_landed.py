"""Hermetic cases for the unlanded-work check.

Each case is a throwaway repository rather than this one's real history: the
scenarios that matter -- a squash, a cherry-pick, a branch left behind -- are
exactly the ones whose evidence a clone, a shallow fetch or a pruned branch
would change underneath the test. The real-history answers the detector was
built against are recorded in aiwatcher_cli/landed.py.
"""

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.landed import (
    PRESENT_PCT_THRESHOLD,
    candidate_branches,
    find_unlanded_work,
    measure_unlanded_change,
    resolve_main_ref,
    unmatched_commits,
)

# Long enough to clear MIN_SIGNIFICANT_LINE_LENGTH, so the lines actually count.
ISOLATION_FIXTURE = (
    "def isolate_state_for_every_test(monkeypatch):\n"
    "    monkeypatch.setenv('AIWATCHER_HOME', str(tmp_path / 'home'))\n"
    "    monkeypatch.setenv('AIWATCHER_STATE_FILE', str(tmp_path / 'state.json'))\n"
)
SECOND_FIXTURE = (
    "def assert_no_writes_to_the_developers_real_home(path):\n"
    "    assert '.aiwatcher' not in str(path.resolve()), 'test wrote to real state'\n"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class UnlandedWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "AIWatcher Test")
        git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        # Every synthetic commit is seconds old, so judge from a later vantage
        # point instead of waiting out MIN_BRANCH_IDLE_DAYS.
        self.later = datetime.now(timezone.utc) + timedelta(days=90)

    def _branch_with_fixture(self, name: str, *, body: str = ISOLATION_FIXTURE, path: str = "conftest.py") -> str:
        git(self.repo, "switch", "-c", name, "main")
        (self.repo / path).write_text(body, encoding="utf-8")
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", f"add {path} on {name}")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "main")
        return sha

    def test_work_main_never_received_is_reported(self) -> None:
        """The PR #63 shape: a branch merged somewhere that main never saw."""
        self._branch_with_fixture("fix/test-isolation")

        report = find_unlanded_work(str(self.repo), now=self.later)

        found = report.unlanded
        self.assertEqual(len(found), 1, msg=f"expected one finding, got {[c.subject for c in found]}")
        self.assertEqual(found[0].branch, "fix/test-isolation")
        self.assertEqual(found[0].present_pct, 0.0)
        self.assertGreater(found[0].lines_absent, 0)

    def test_a_cherry_picked_commit_is_cleared_without_reading_files(self) -> None:
        """What PR #211 needed: same change on main under a different SHA."""
        sha = self._branch_with_fixture("claude/fresh-start-card-project")
        git(self.repo, "cherry-pick", sha)

        self.assertEqual(unmatched_commits(str(self.repo), "claude/fresh-start-card-project", "main"), [])
        self.assertEqual(find_unlanded_work(str(self.repo), now=self.later).unlanded, [])

    def test_a_squash_merge_survives_patch_identity_and_is_cleared_by_its_lines(self) -> None:
        """What PRs #175-#178 needed: content on main, but as a different change."""
        git(self.repo, "switch", "-c", "codex/verification-scope", "main")
        (self.repo / "conftest.py").write_text(ISOLATION_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "conftest.py")
        git(self.repo, "commit", "-m", "first half")
        (self.repo / "conftest.py").write_text(ISOLATION_FIXTURE + SECOND_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "conftest.py")
        git(self.repo, "commit", "-m", "second half")
        git(self.repo, "switch", "main")
        # A squash lands the combined content as one new commit, so neither
        # original commit has a patch twin on main.
        (self.repo / "conftest.py").write_text(ISOLATION_FIXTURE + SECOND_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "conftest.py")
        git(self.repo, "commit", "-m", "Classify verification scope conservatively (#176)")

        suspected = unmatched_commits(str(self.repo), "codex/verification-scope", "main")
        self.assertEqual(len(suspected), 2, msg="patch identity should still suspect both commits")

        self.assertEqual(find_unlanded_work(str(self.repo), now=self.later).unlanded, [])

    def test_a_branch_still_being_worked_on_is_left_alone(self) -> None:
        """Unmerged commits on live work are work in progress, not losses."""
        self._branch_with_fixture("feat/in-progress")

        report = find_unlanded_work(str(self.repo), now=datetime.now(timezone.utc))

        self.assertEqual(report.unlanded, [])
        self.assertEqual(report.branches_scanned, 0)
        self.assertEqual(report.branches_skipped_active, 1)

    def test_a_branch_merged_normally_is_never_a_candidate(self) -> None:
        sha = self._branch_with_fixture("feat/landed")
        git(self.repo, "merge", "--no-ff", sha, "-m", "Merge pull request #1")

        self.assertNotIn("feat/landed", candidate_branches(str(self.repo), "main"))
        self.assertEqual(find_unlanded_work(str(self.repo), now=self.later).unlanded, [])

    def test_the_biggest_loss_is_reported_first(self) -> None:
        self._branch_with_fixture("fix/one-liner", body="x = 'a single added line of code'\n", path="small.py")
        self._branch_with_fixture("fix/test-isolation", body=ISOLATION_FIXTURE + SECOND_FIXTURE)

        found = find_unlanded_work(str(self.repo), now=self.later).unlanded

        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].branch, "fix/test-isolation")
        self.assertGreater(found[0].lines_absent, found[1].lines_absent)

    def test_a_file_main_does_have_is_measured_against_its_current_contents(self) -> None:
        """Presence is read from main's files, not from who blame credits."""
        sha = self._branch_with_fixture("fix/partial")
        git(self.repo, "switch", "main")
        # Main holds most of the change, under no shared history at all.
        (self.repo / "conftest.py").write_text(ISOLATION_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "conftest.py")
        git(self.repo, "commit", "-m", "same content, unrelated commit")

        change = measure_unlanded_change(str(self.repo), sha, branch="fix/partial", main_ref="main")

        self.assertTrue(change.measurable)
        self.assertEqual(change.present_pct, 100.0)
        self.assertFalse(change.looks_unlanded)

    def test_a_report_without_a_main_ref_says_so_instead_of_guessing(self) -> None:
        bare = Path(self.temp.name) / "empty"
        bare.mkdir()
        git(bare, "init", "-b", "trunk")

        self.assertIsNone(resolve_main_ref(str(bare)))
        report = find_unlanded_work(str(bare), now=self.later)
        self.assertEqual(report.unlanded, [])
        self.assertTrue(any("nothing to compare" in note for note in report.notes))

    def test_work_another_branch_merged_outranks_a_branch_nobody_merged(self) -> None:
        """The PR #63 shape must beat a bigger abandoned experiment."""
        small = self._branch_with_fixture("fix/test-isolation")
        # A parent branch absorbs the small change and is itself never merged:
        # exactly what a stacked pull request leaves behind.
        git(self.repo, "switch", "-c", "codex/parent", "main")
        (self.repo / "parent.py").write_text("PARENT = 'a line of parent work'\n", encoding="utf-8")
        git(self.repo, "add", "parent.py")
        git(self.repo, "commit", "-m", "parent work")
        git(self.repo, "merge", "--no-ff", small, "-m", "Merge pull request #63")
        git(self.repo, "switch", "main")
        self._branch_with_fixture(
            "feat/tasks-view",
            body="".join(f"ABANDONED_SETTING_{index} = 'an experiment nobody merged'\n" for index in range(40)),
            path="tasks.py",
        )

        report = find_unlanded_work(str(self.repo), now=self.later)

        stranded = report.stranded
        self.assertTrue(stranded, msg="the merged-elsewhere commit should be reported")
        # Both branches hold the commit, so which one is named is arbitrary;
        # what matters is that the pair is reported together.
        self.assertEqual(
            {stranded[0].branch, *stranded[0].merged_into},
            {"fix/test-isolation", "codex/parent"},
        )
        # The abandoned branch is larger, and must still rank below it.
        abandoned = report.unmerged_anywhere
        self.assertIn("feat/tasks-view", {change.branch for change in abandoned})
        self.assertGreater(abandoned[0].lines_absent, stranded[0].lines_absent)
        self.assertIs(report.unlanded[0], stranded[0])

    def test_code_that_moved_to_another_file_is_not_reported_as_missing(self) -> None:
        """PR #185's one-line Windows fix: on main, in a file it did not touch."""
        git(self.repo, "switch", "-c", "fix/moved", "main")
        (self.repo / "cli.py").write_text(ISOLATION_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "cli.py")
        git(self.repo, "commit", "-m", "add the helper to cli.py")
        moved_sha = git(self.repo, "rev-parse", "HEAD")
        # Merged into a branch that never reached main, so it is on the shown
        # path -- the only path a move is probed on.
        git(self.repo, "switch", "-c", "codex/parent", "main")
        git(self.repo, "merge", "--no-ff", moved_sha, "-m", "Merge pull request #185")
        git(self.repo, "switch", "main")
        # Main has the same lines, in a module the commit never touched, so a
        # same-path check sees an empty file and scores zero.
        (self.repo / "delivery_tracking.py").write_text(ISOLATION_FIXTURE, encoding="utf-8")
        git(self.repo, "add", "delivery_tracking.py")
        git(self.repo, "commit", "-m", "the helper now lives here")

        report = find_unlanded_work(str(self.repo), now=self.later)

        self.assertEqual(report.unlanded, [])
        moved = [change for change in report.changes if change.moved_in_main]
        self.assertTrue(moved, msg="the moved change should be recognised, not just dropped")
        self.assertIn("moved rather than went missing", moved[0].reason or "")
        self.assertEqual(moved[0].present_pct, 0.0)

    def test_probing_for_a_move_is_skipped_when_the_change_is_clearly_present(self) -> None:
        sha = self._branch_with_fixture("fix/present")
        git(self.repo, "cherry-pick", sha)

        change = measure_unlanded_change(str(self.repo), sha, branch="fix/present", main_ref="main")

        self.assertEqual(change.present_pct, 100.0)
        self.assertEqual(change.probes, 0)

    def test_the_threshold_leaves_room_on_both_sides(self) -> None:
        """Guards the measured gap: squashes read 88-100%, losses 0-21%."""
        self.assertGreater(PRESENT_PCT_THRESHOLD, 21.0)
        self.assertLess(PRESENT_PCT_THRESHOLD, 88.0)


if __name__ == "__main__":
    unittest.main()
