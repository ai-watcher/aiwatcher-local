from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import local_state
from aiwatcher_cli import delivery_tracking
from aiwatcher_cli.delivery_tracking import (
    COMMIT_HOOK_MARKER,
    delivery_tracking_status,
    install_commit_hook,
    observed_repositories,
    remove_commit_hook,
    sync_delivery_tracking,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "AIWatcher Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")


def session(path: Path, session_id: str = "session-1") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        raw_cwd=str(path),
        project_path=str(path),
    )


class AutomaticDeliveryTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        delivery_tracking._SYNC_CACHE.clear()
        delivery_tracking._DISCOVERY_CURSOR.clear()
        delivery_tracking._DISCOVERY_SIGNATURE.clear()

    def test_disabled_tracking_never_modifies_an_observed_repository(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)

        status = sync_delivery_tracking([session(repo)])

        self.assertFalse(status["enabled"])
        self.assertEqual(status["tracked_repositories"], 0)
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())

    def test_one_enable_configures_current_and_future_observed_repositories(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        init_repo(first)
        init_repo(second)
        local_state.record_delivery_tracking_settings({"enabled": True})

        initial = sync_delivery_tracking([session(first)])
        later = sync_delivery_tracking([session(first), session(second, "session-2")])

        self.assertTrue(initial["enabled"])
        self.assertEqual(initial["tracked_repositories"], 1)
        self.assertEqual(later["tracked_repositories"], 2)
        for repo in (first, second):
            hook = (repo / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
            self.assertIn(COMMIT_HOOK_MARKER, hook)

    def test_branches_and_linked_worktrees_share_one_repository_hook(self) -> None:
        repo = self.root / "repo"
        worktree = self.root / "feature-worktree"
        init_repo(repo)
        git(repo, "worktree", "add", "-b", "feature", str(worktree))

        repositories = observed_repositories([
            session(repo),
            session(worktree, "session-2"),
        ])

        self.assertEqual(len(repositories), 1)
        self.assertEqual(
            Path(repositories[0]["hook_path"]).resolve(),
            (repo / ".git" / "hooks" / "post-commit").resolve(),
        )

    def test_automatic_setup_leaves_an_existing_hook_unchanged(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho existing-hook\n", encoding="utf-8")
        local_state.record_delivery_tracking_settings({"enabled": True})
        original = hook.read_bytes()

        status = sync_delivery_tracking([session(repo)])

        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["needs_attention"], 1)
        self.assertEqual(hook.read_bytes(), original)
        self.assertEqual(status["sync_results"][0]["status"], "existing_hook_requires_confirmation")

    def test_custom_hooks_path_is_never_modified_automatically(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        git(repo, "config", "core.hooksPath", ".githooks")
        custom_hook = repo / ".githooks" / "post-commit"
        custom_hook.parent.mkdir()
        custom_hook.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        original = custom_hook.read_bytes()
        local_state.record_delivery_tracking_settings({"enabled": True})

        status = sync_delivery_tracking([session(repo)])

        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["repositories"][0]["issue"], "custom_hooks_path")
        self.assertEqual(status["sync_results"][0]["status"], "custom_hooks_path")
        self.assertEqual(custom_hook.read_bytes(), original)
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())

    def test_dormant_standard_hook_is_not_counted_with_custom_hooks_path(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        standard = repo / ".git" / "hooks" / "post-commit"
        standard.write_text(
            "#!/bin/sh\n" + COMMIT_HOOK_MARKER + "\naiwatcher commit-receipt\n# <<< aiwatcher commit receipt <<<\n",
            encoding="utf-8",
        )
        git(repo, "config", "core.hooksPath", ".githooks")
        local_state.record_delivery_tracking_settings({"enabled": True})

        status = delivery_tracking_status([session(repo)])

        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["repositories"][0]["issue"], "custom_hooks_path")

    def test_explicit_uninstall_removes_marker_from_active_custom_hook(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        git(repo, "config", "core.hooksPath", ".githooks")
        hook = repo / ".githooks" / "post-commit"
        hook.parent.mkdir()
        hook.write_text(
            "#!/bin/sh\necho existing\n\n"
            + COMMIT_HOOK_MARKER
            + "\naiwatcher commit-receipt\n# <<< aiwatcher commit receipt <<<\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        status = delivery_tracking_status([session(repo)])
        self.assertEqual(status["tracked_repositories"], 1)
        self.assertEqual(status["needs_attention"], 0)

        result = remove_commit_hook(str(repo))

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "removed_block")
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\necho existing\n")

    def test_explicit_setup_refuses_a_non_shell_hook_without_changes(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/usr/bin/env python3\nprint('custom')\n", encoding="utf-8")
        original = hook.read_bytes()

        result = install_commit_hook(str(repo), allow_existing=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unsupported_existing_hook")
        self.assertEqual(hook.read_bytes(), original)

    def test_automatic_update_refuses_legacy_markers_inside_python_hook(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/usr/bin/env python3\nprint('custom')\n# >>> aiwatcher commit receipt >>>\n"
            "print('legacy marker')\n# <<< aiwatcher commit receipt <<<\n",
            encoding="utf-8",
        )
        original = hook.read_bytes()
        local_state.record_delivery_tracking_settings({"enabled": True})

        status = sync_delivery_tracking([session(repo)])

        self.assertEqual(status["sync_results"][0]["status"], "unsupported_existing_hook")
        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["repositories"][0]["issue"], "unsupported_existing_hook")
        self.assertEqual(hook.read_bytes(), original)

    def test_explicit_setup_can_compose_with_a_confirmed_shell_hook(self) -> None:
        repo = self.root / "repo with spaces"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

        result = install_commit_hook(str(repo), command="aiwatcher", allow_existing=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "installed_alongside_existing")
        contents = hook.read_text(encoding="utf-8")
        self.assertIn("echo existing", contents)
        self.assertEqual(contents.count(COMMIT_HOOK_MARKER), 1)

    def test_updating_a_symlinked_hook_preserves_the_symlink_and_target(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        target = self.root / "managed-post-commit"
        target.write_text(
            "#!/bin/sh\n" + delivery_tracking.commit_hook_body("/old/aiwatcher"),
            encoding="utf-8",
        )
        target.chmod(0o755)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.symlink_to(target)
        original = target.read_bytes()

        result = install_commit_hook(str(repo), command="/new/aiwatcher")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "symlink_hook_requires_manual_update")
        self.assertTrue(hook.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_uninstall_removes_dormant_standard_hook_after_hooks_path_changes(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        installed = install_commit_hook(str(repo), command="aiwatcher")
        self.assertTrue(installed["ok"])
        standard = repo / ".git" / "hooks" / "post-commit"
        git(repo, "config", "core.hooksPath", ".githooks")
        custom = repo / ".githooks" / "post-commit"
        custom.parent.mkdir()
        custom.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")

        result = remove_commit_hook(str(repo))

        self.assertTrue(result["ok"])
        self.assertFalse(standard.exists())
        self.assertEqual(custom.read_text(encoding="utf-8"), "#!/bin/sh\necho custom\n")

    def test_marker_only_hook_is_not_reported_as_tracked(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n" + COMMIT_HOOK_MARKER + "\necho not-a-receipt\n"
            "# <<< aiwatcher commit receipt <<<\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        status = delivery_tracking_status([session(repo)])

        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["needs_attention"], 1)
        self.assertEqual(status["repositories"][0]["issue"], "invalid_aiwatcher_hook")

    def test_command_shaped_fake_hook_is_not_reported_as_tracked(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n" + COMMIT_HOOK_MARKER + "\n"
            "false -m aiwatcher_cli commit-receipt\n"
            "# <<< aiwatcher commit receipt <<<\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        status = delivery_tracking_status([session(repo)])

        self.assertEqual(status["tracked_repositories"], 0)
        self.assertEqual(status["needs_attention"], 1)
        self.assertEqual(status["repositories"][0]["issue"], "invalid_aiwatcher_hook")

    def test_concurrent_installers_leave_one_complete_marker_block(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: install_commit_hook(str(repo), command="aiwatcher"), range(20)))

        hook = repo / ".git" / "hooks" / "post-commit"
        contents = hook.read_text(encoding="utf-8")
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(contents.count(COMMIT_HOOK_MARKER), 1)
        self.assertEqual(contents.count("# <<< aiwatcher commit receipt <<<"), 1)

    def test_event_level_checkout_is_discovered(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        init_repo(first)
        init_repo(second)
        event = SimpleNamespace(raw_cwd=str(second), checkout_path=None, project_path=None)

        repositories = observed_repositories([session(first), event])

        self.assertEqual(len(repositories), 2)

    def test_status_cache_cannot_suppress_sync_for_a_new_event_checkout(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        init_repo(first)
        init_repo(second)
        local_state.record_delivery_tracking_settings({"enabled": True})
        delivery_tracking_status([session(first)], force=False)
        event = SimpleNamespace(raw_cwd=str(second), checkout_path=None, project_path=None)

        status = sync_delivery_tracking([session(first), event], force=False)

        self.assertEqual(status["tracked_repositories"], 2)
        self.assertTrue((second / ".git" / "hooks" / "post-commit").exists())

    def test_changed_candidate_set_resets_batch_to_newest_repository(self) -> None:
        repos = []
        for index in range(4):
            repo = self.root / f"repo-{index}"
            init_repo(repo)
            repos.append(repo)
        local_state.record_delivery_tracking_settings({"enabled": True})
        items = [SimpleNamespace(raw_cwd=str(repo), checkout_path=None, project_path=None) for repo in repos[:3]]
        newest = SimpleNamespace(raw_cwd=str(repos[3]), checkout_path=None, project_path=None)

        with patch("aiwatcher_cli.delivery_tracking._AUTOMATIC_CANDIDATE_LIMIT", 2):
            sync_delivery_tracking(items, force=True)
            status = sync_delivery_tracking([newest, *items], force=True)

        self.assertTrue((repos[3] / ".git" / "hooks" / "post-commit").exists())
        self.assertIn(
            repos[3].resolve(),
            {Path(row["checkout_path"]).resolve() for row in status["repositories"]},
        )

    def test_linked_worktree_commit_executes_the_shared_hook(self) -> None:
        repo = self.root / "repo"
        worktree = self.root / "feature worktree"
        receipt = self.root / "receipt.txt"
        init_repo(repo)
        git(repo, "worktree", "add", "-b", "feature", str(worktree))
        command = f"printf linked-receipt >> '{receipt}'"
        result = install_commit_hook(str(repo), command=command)
        self.assertTrue(result["ok"])
        (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(worktree, "add", "feature.txt")

        git(worktree, "commit", "-m", "feature")

        self.assertEqual(receipt.read_text(encoding="utf-8"), "linked-receipt")

    def test_windows_console_entrypoint_uses_git_bash_safe_slashes(self) -> None:
        with patch("aiwatcher_cli.delivery_tracking.shutil.which", return_value=r"C:\Users\Me\bin\aiwatcher.exe"), patch(
            "aiwatcher_cli.delivery_tracking.os.name", "nt",
        ):
            command = delivery_tracking.cli_command_for_current_install()

        self.assertNotIn("\\", command)
        self.assertIn("C:/Users/Me/bin/aiwatcher.exe", command)

    def test_repository_discovery_has_no_silent_hundred_repo_cap(self) -> None:
        items = [SimpleNamespace(raw_cwd=f"/tmp/repo-{index}", checkout_path=None, project_path=None) for index in range(105)]

        def identity(path: str):
            index = path.rsplit("-", 1)[-1]
            return SimpleNamespace(repository_id=f"repo-{index}", checkout_path=path)

        with patch("aiwatcher_cli.delivery_tracking.resolve_git_identity", side_effect=identity), patch(
            "aiwatcher_cli.delivery_tracking.commit_hook_path",
            side_effect=lambda path: f"{path}/.git/hooks/post-commit",
        ), patch("aiwatcher_cli.delivery_tracking.custom_hooks_configuration", return_value=None):
            repositories = observed_repositories(items)

        self.assertEqual(len(repositories), 105)

    def test_disabling_stops_management_without_silently_removing_hooks(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        local_state.record_delivery_tracking_settings({"enabled": True})
        sync_delivery_tracking([session(repo)])
        local_state.record_delivery_tracking_settings({"enabled": False})

        status = delivery_tracking_status([session(repo)])

        self.assertFalse(status["enabled"])
        self.assertEqual(status["tracked_repositories"], 1)
        self.assertTrue((repo / ".git" / "hooks" / "post-commit").exists())


if __name__ == "__main__":
    unittest.main()
