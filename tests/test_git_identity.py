from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli.git_identity import (
    _BIRTH_CACHE,
    _filesystem_identity,
    _persistent_generation_marker,
    identity_for_session,
    resolve_git_identity,
)
from aiwatcher_cli.ledger import _event_repo, build_ledger
from aiwatcher_cli.scanner import LocalEvent, LocalSession


def run(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def init_repo(path: str) -> None:
    run(path, "init", "-q")
    run(path, "config", "user.name", "AIWatcher Test")
    run(path, "config", "user.email", "aiwatcher@example.com")
    Path(path, "README.md").write_text("identity\n", encoding="utf-8")
    run(path, "add", "README.md")
    run(path, "commit", "-q", "-m", "root")


class GitIdentityTests(unittest.TestCase):
    def test_identity_without_birth_time_survives_ctime_changes(self) -> None:
        before = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=13)
        after = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=99)

        with (
            patch("aiwatcher_cli.git_identity._filesystem_birth_marker", return_value=None),
            patch(
                "aiwatcher_cli.git_identity._persistent_generation_marker",
                return_value="a" * 32,
            ),
            patch("aiwatcher_cli.git_identity.os.stat", side_effect=[before, after]),
        ):
            first = _filesystem_identity("/repo/.git")
            second = _filesystem_identity("/repo/.git")

        self.assertEqual(first, f"inode:7:11:generation:{'a' * 32}")
        self.assertEqual(second, first)

    def test_generation_marker_prevents_inode_reuse_from_inheriting_identity(self) -> None:
        stat = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=13)
        with (
            patch("aiwatcher_cli.git_identity._filesystem_birth_marker", return_value=None),
            patch("aiwatcher_cli.git_identity.os.stat", return_value=stat),
            patch(
                "aiwatcher_cli.git_identity._persistent_generation_marker",
                side_effect=["a" * 32, "b" * 32],
            ),
        ):
            original = _filesystem_identity("/repo/.git")
            replacement = _filesystem_identity("/repo/.git")

        self.assertNotEqual(original, replacement)

    def test_read_only_fallback_fails_closed_when_ctime_changes(self) -> None:
        before = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=13)
        after = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=99)
        with (
            patch("aiwatcher_cli.git_identity._filesystem_birth_marker", return_value=None),
            patch("aiwatcher_cli.git_identity._persistent_generation_marker", return_value=None),
            patch("aiwatcher_cli.git_identity.os.stat", side_effect=[before, after]),
        ):
            first = _filesystem_identity("/repo/.git")
            second = _filesystem_identity("/repo/.git")

        self.assertNotEqual(first, second)

    def test_marker_error_uses_generation_safe_ctime_not_path(self) -> None:
        stat = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=13)
        with (
            patch("aiwatcher_cli.git_identity._filesystem_birth_marker", return_value=None),
            patch(
                "aiwatcher_cli.git_identity._persistent_generation_marker",
                side_effect=OSError("marker unavailable"),
            ),
            patch("aiwatcher_cli.git_identity.os.stat", return_value=stat),
        ):
            marker = _filesystem_identity("/repo/.git")

        self.assertEqual(marker, "inode:7:11:ctime:13")

    def test_failed_marker_write_is_removed_for_a_later_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir, "aiwatcher-generation-v1")
            with patch("aiwatcher_cli.git_identity.os.write", side_effect=OSError("disk full")):
                value = _persistent_generation_marker(temp_dir)

            self.assertIsNone(value)
            self.assertFalse(marker.exists())
            self.assertIsNotNone(_persistent_generation_marker(temp_dir))

    def test_concurrent_marker_creation_waits_for_the_winner(self) -> None:
        value = "a" * 32
        with (
            patch(
                "aiwatcher_cli.git_identity._read_generation_marker",
                side_effect=[None, None, None, value],
            ),
            patch("aiwatcher_cli.git_identity.os.open", side_effect=FileExistsError),
            patch("aiwatcher_cli.git_identity.time.sleep"),
        ):
            observed = _persistent_generation_marker("/repo/.git")

        self.assertEqual(observed, value)

    def test_generation_marker_is_stable_and_private_to_git_admin_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _persistent_generation_marker(temp_dir)
            second = _persistent_generation_marker(temp_dir)
            marker = Path(temp_dir, "aiwatcher-generation-v1")
            mode = marker.stat().st_mode & 0o777

        self.assertIsNotNone(first)
        self.assertEqual(second, first)
        self.assertEqual(len(str(first)), 32)
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_generation_fallback_does_not_transfer_repository_authorization(self) -> None:
        from aiwatcher_cli import local_state

        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            repo = str(Path(temp_dir, "repo"))
            retired = str(Path(temp_dir, "retired-git"))
            Path(repo).mkdir()
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}),
                patch("aiwatcher_cli.git_identity._filesystem_birth_marker", return_value=None),
            ):
                init_repo(repo)
                original = resolve_git_identity(repo)
                local_state.record_analyst_consent(repo, allowed=True)
                local_state.record_analyst_contents(repo, allowed=True)

                os.rename(Path(repo, ".git"), retired)
                init_repo(repo)
                replacement = resolve_git_identity(repo)
                inherited_consent = local_state.analyst_consent(repo)
                inherited_contents = local_state.analyst_contents_allowed(repo)

        assert original is not None and replacement is not None
        self.assertNotEqual(original.repository_id, replacement.repository_id)
        self.assertNotEqual(original.checkout_id, replacement.checkout_id)
        self.assertIsNone(inherited_consent)
        self.assertFalse(inherited_contents)

    def test_linux_identity_includes_birth_time_when_inode_can_be_reused(self) -> None:
        stat = SimpleNamespace(st_dev=7, st_ino=11, st_ctime_ns=13)
        completed = subprocess.CompletedProcess(
            ["stat"],
            0,
            stdout="2026-09-30 02:06:00.123456789 +0000\n",
            stderr="",
        )
        _BIRTH_CACHE.clear()
        with (
            patch("aiwatcher_cli.git_identity.sys.platform", "linux"),
            patch("aiwatcher_cli.git_identity.os.name", "posix"),
            patch("aiwatcher_cli.git_identity.os.stat", return_value=stat),
            patch("aiwatcher_cli.git_identity.subprocess.run", return_value=completed),
        ):
            marker = _filesystem_identity("/repo/.git")

        self.assertEqual(
            marker,
            "inode:7:11:born:2026-09-30 02:06:00.123456789 +0000",
        )

    def test_repository_and_checkout_identity_survive_directory_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            original = str(Path(temp_dir, "original"))
            moved = str(Path(temp_dir, "moved"))
            Path(original).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(original)
                before = resolve_git_identity(original)
                os.rename(original, moved)
                after = resolve_git_identity(moved)

        assert before is not None and after is not None
        self.assertEqual(before.repository_id, after.repository_id)
        self.assertEqual(before.checkout_id, after.checkout_id)
        self.assertEqual(after.checkout_path, os.path.realpath(moved))

    def test_linked_worktrees_share_repository_but_not_checkout_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            main = str(Path(temp_dir, "main"))
            worktree = str(Path(temp_dir, "review"))
            Path(main).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(main)
                run(main, "worktree", "add", "-q", "-b", "review", worktree)
                main_id = resolve_git_identity(main)
                worktree_id = resolve_git_identity(worktree)

        self.assertIsNotNone(main_id)
        self.assertIsNotNone(worktree_id)
        assert main_id is not None and worktree_id is not None
        self.assertEqual(main_id.repository_id, worktree_id.repository_id)
        self.assertEqual(main_id.repository_lineage_id, worktree_id.repository_lineage_id)
        self.assertNotEqual(main_id.checkout_id, worktree_id.checkout_id)
        self.assertNotEqual(main_id.checkout_path, worktree_id.checkout_path)

    def test_clones_have_distinct_local_repositories_but_shared_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            origin = str(Path(temp_dir, "origin"))
            clone = str(Path(temp_dir, "clone"))
            Path(origin).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(origin)
                run(temp_dir, "clone", "-q", origin, clone)
                origin_id = resolve_git_identity(origin)
                clone_id = resolve_git_identity(clone)

        assert origin_id is not None and clone_id is not None
        self.assertNotEqual(origin_id.repository_id, clone_id.repository_id)
        self.assertNotEqual(origin_id.checkout_id, clone_id.checkout_id)
        self.assertEqual(origin_id.repository_lineage_id, clone_id.repository_lineage_id)

    def test_forks_with_the_same_root_but_different_remotes_do_not_share_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            source = str(Path(temp_dir, "source"))
            first = str(Path(temp_dir, "first"))
            second = str(Path(temp_dir, "second"))
            Path(source).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(source)
                run(temp_dir, "clone", "-q", source, first)
                run(temp_dir, "clone", "-q", source, second)
                run(first, "remote", "set-url", "origin", "https://github.com/acme/one.git")
                run(second, "remote", "set-url", "origin", "git@github.com:acme/two.git")
                first_id = resolve_git_identity(first)
                second_id = resolve_git_identity(second)

        assert first_id is not None and second_id is not None
        self.assertNotEqual(first_id.repository_lineage_id, second_id.repository_lineage_id)

    def test_lineage_refreshes_when_origin_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            repo = str(Path(temp_dir, "repo"))
            Path(repo).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(repo)
                run(repo, "remote", "add", "origin", "https://github.com/acme/one.git")
                before = resolve_git_identity(repo)
                run(repo, "remote", "set-url", "origin", "https://github.com/acme/two.git")
                after = resolve_git_identity(repo)

        assert before is not None and after is not None
        self.assertNotEqual(before.repository_lineage_id, after.repository_lineage_id)

    def test_session_prefers_observed_linked_worktree_and_rejects_other_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            main = str(Path(temp_dir, "main"))
            worktree = str(Path(temp_dir, "review"))
            clone = str(Path(temp_dir, "clone"))
            Path(main).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(main)
                run(main, "worktree", "add", "-q", "-b", "review", worktree)
                run(temp_dir, "clone", "-q", main, clone)
                linked = identity_for_session(main, worktree)
                mismatched = identity_for_session(main, clone)
                main_id = resolve_git_identity(main)

                payload = LocalSession(
                    session_id="conflict", tool="codex-cli", project_path=main, raw_cwd=clone,
                ).to_json()

        assert linked is not None and mismatched is not None and main_id is not None
        self.assertEqual(linked.checkout_path, os.path.realpath(worktree))
        self.assertEqual(linked.identity_source, "observed_git")
        self.assertEqual(mismatched.checkout_id, main_id.checkout_id)
        self.assertEqual(mismatched.identity_source, "identity_conflict")

        self.assertEqual(payload["identity_source"], "identity_conflict")
        self.assertIsNone(payload["repository_id"])
        self.assertIsNone(payload["checkout_id"])

    def test_cached_session_restores_observed_worktree_for_evidence(self) -> None:
        from aiwatcher_cli import ui
        from aiwatcher_cli.outcome_evidence import _checkout_root

        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            main = str(Path(temp_dir, "main"))
            worktree = str(Path(temp_dir, "review"))
            Path(main).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(main)
                run(main, "worktree", "add", "-q", "-b", "review", worktree)
                saved = LocalSession(
                    session_id="s1", tool="codex-cli", project_path=main, raw_cwd=worktree,
                ).to_json()
                restored = ui._session_from_json(saved)
                checkout = _checkout_root(restored) if restored else None

        self.assertIsNotNone(restored)
        self.assertEqual(restored.raw_cwd, worktree)
        self.assertEqual(checkout, os.path.realpath(worktree))

    def test_ui_groups_linked_worktrees_as_one_repository(self) -> None:
        from aiwatcher_cli import ui

        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            main = str(Path(temp_dir, "main"))
            worktree = str(Path(temp_dir, "review"))
            Path(main).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(main)
                run(main, "worktree", "add", "-q", "-b", "review", worktree)
                rows = [
                    LocalSession("s1", "codex-cli", project_path=main, raw_cwd=main),
                    LocalSession("s2", "codex-cli", project_path=worktree, raw_cwd=worktree),
                ]
                projects = ui.group_projects(rows)
                with patch.object(ui, "rows_for_window", return_value=rows):
                    detail = ui.build_project_detail(main)

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["sessions"], 2)
        self.assertEqual(detail["totals"]["sessions"], 2)
        self.assertEqual({row["session_id"] for row in detail["sessions"]}, {"s1", "s2"})

    def test_ui_isolates_session_when_observed_checkout_conflicts(self) -> None:
        from aiwatcher_cli import ui

        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            first = str(Path(temp_dir, "first"))
            second = str(Path(temp_dir, "second"))
            Path(first).mkdir()
            Path(second).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(first)
                init_repo(second)
                rows = [
                    LocalSession("normal", "codex-cli", project_path=first, raw_cwd=first),
                    LocalSession("conflict", "codex-cli", project_path=first, raw_cwd=second),
                ]
                projects = ui.group_projects(rows)
                self.assertIsNone(ui._fresh_start_session_skip_key(rows[1]))
                conflict_id = next(
                    str(item["id"]) for item in projects if str(item["id"]).startswith("conflict:")
                )
                with patch.object(ui, "rows_for_window", return_value=rows):
                    normal_detail = ui.build_project_detail(first)
                    conflict_detail = ui.build_project_detail(conflict_id)

        self.assertEqual(len(projects), 2)
        self.assertEqual(sorted(int(item["sessions"]) for item in projects), [1, 1])
        self.assertEqual(len({str(item["id"]) for item in projects}), 2)
        self.assertEqual([row["session_id"] for row in normal_detail["sessions"]], ["normal"])
        self.assertEqual([row["session_id"] for row in conflict_detail["sessions"]], ["conflict"])

    def test_session_json_carries_opaque_ids_without_remote_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            repo = str(Path(temp_dir, "repo"))
            Path(repo).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(repo)
                run(repo, "remote", "add", "origin", "https://token@example.com/acme/private.git")
                payload = LocalSession(
                    session_id="s1", tool="codex-cli", project_path=repo, raw_cwd=repo,
                ).to_json()
                stored = Path(state).read_text(encoding="utf-8")

        self.assertTrue(str(payload["repository_id"]).startswith("repository-v1-"))
        self.assertTrue(str(payload["checkout_id"]).startswith("checkout-v1-"))
        self.assertTrue(str(payload["repository_lineage_id"]).startswith("lineage-v1-"))
        self.assertNotIn("https://token@example.com", json.dumps(payload))
        self.assertNotIn("https://token@example.com", stored)
        self.assertNotIn("private.git", stored)

    def test_event_identity_uses_observed_worktree_not_prompt_project_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            main = str(Path(temp_dir, "main"))
            worktree = str(Path(temp_dir, "review"))
            Path(main).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(main)
                run(main, "worktree", "add", "-q", "-b", "review", worktree)
                now = datetime.now(timezone.utc)
                event_time = now + timedelta(minutes=1)
                commit_time = now + timedelta(minutes=2)
                Path(worktree, "fix.py").write_text("fixed\n", encoding="utf-8")
                run(worktree, "add", "fix.py")
                stamp = commit_time.strftime("%Y-%m-%dT%H:%M:%S%z")
                env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
                subprocess.run(
                    ["git", "-C", worktree, "commit", "-q", "-m", "worktree change"],
                    check=True,
                    env=env,
                )
                event = LocalEvent(
                    event_id="e1",
                    session_id="s1",
                    tool="codex-cli",
                    event_type="model_usage",
                    timestamp=event_time,
                    project_path=main,
                    raw_cwd=worktree,
                    cost_usd=3.0,
                )
                payload = event.to_json()
                ledger = build_ledger([event], days=7, now=now + timedelta(minutes=3))

        self.assertEqual(payload["checkout_path"], os.path.realpath(worktree))
        self.assertEqual(payload["identity_source"], "observed_git")
        changes_by_subject = {change.subject: change for change in ledger.changes}
        self.assertIn("worktree change", changes_by_subject)
        self.assertEqual(changes_by_subject["worktree change"].cost_usd, 3.0)

    def test_event_repo_cache_keeps_project_fallbacks_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            first = str(Path(temp_dir, "first"))
            second = str(Path(temp_dir, "second"))
            Path(first).mkdir()
            Path(second).mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                init_repo(first)
                init_repo(second)
                cache: dict[tuple[str, str], str | None] = {}
                first_root = _event_repo(
                    LocalEvent(
                        "e1", "s1", "codex-cli", "model_usage",
                        project_path=first, raw_cwd=temp_dir,
                    ),
                    cache,
                )
                second_root = _event_repo(
                    LocalEvent(
                        "e2", "s2", "codex-cli", "model_usage",
                        project_path=second, raw_cwd=temp_dir,
                    ),
                    cache,
                )

        self.assertEqual(first_root, os.path.realpath(first))
        self.assertEqual(second_root, os.path.realpath(second))

    def test_missing_git_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = str(Path(temp_dir, "state.json"))
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state}):
                self.assertIsNone(resolve_git_identity(temp_dir))
