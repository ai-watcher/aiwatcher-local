import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from aiwatcher_cli import ai_assist, ui
from aiwatcher_cli.landed import LandingReport, UnlandedChange
from aiwatcher_cli.optimize_review import session_review_identity, REVIEW_BATCH_SIZE
from aiwatcher_cli.processes import RuntimeProcessInventory
from aiwatcher_cli.scanner import LocalSession


class OptimizeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(timezone.utc)
        ui._WORKSPACE_SIZE_CACHE.clear()

    def git(self, path: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def nested_codex_worktree(self) -> tuple[Path, Path, Path]:
        root = Path(self.tmp.name) / ".codex" / "worktrees"
        owner = Path(self.tmp.name) / "owner"
        checkout = root / "abc123" / "demo"
        owner.mkdir()
        checkout.parent.mkdir(parents=True)
        self.git(owner, "init", "-b", "main")
        self.git(owner, "config", "user.name", "Test")
        self.git(owner, "config", "user.email", "test@example.test")
        (owner / "payload.bin").write_bytes(b"x" * 4096)
        self.git(owner, "add", "payload.bin")
        self.git(owner, "commit", "-m", "baseline")
        self.git(owner, "worktree", "add", "-b", "codex/nested-review", str(checkout))
        old = (self.now - timedelta(days=2)).timestamp()
        os.utime(checkout, (old, old))
        return root, owner, checkout

    def session(self, sid, source="cli", hours=8, final=True):
        path = Path(self.tmp.name) / f"rollout-{sid}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/owning-project", "originator": "Codex Desktop", "source": source}},
            {"type": "turn_context", "payload": {"cwd": "/another-checkout"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "PRIVATE FINAL TEXT"}]}},
            {"type": "event_msg", "timestamp": (self.now - timedelta(hours=hours)).isoformat(), "payload": {"type": "task_complete" if final else "task_started", "last_agent_message": "PRIVATE FINAL TEXT"}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return LocalSession(session_id=sid, tool="codex-cli", project_path="/owning-project", title=sid,
                            source_path=str(path), updated_at=self.now - timedelta(hours=hours), tokens_in=400_000)

    def landing_report(self, changes=1, project="/owning-project"):
        """A stranded-work scan result, so these tests never shell out to git.

        The detector itself is covered by tests/test_landed.py against purpose-built
        repositories; what matters here is the review machinery around a candidate.
        """
        report = LandingReport(repo=project, main_ref="origin/main", branches_scanned=3, commits_examined=changes)
        for index in range(changes):
            report.changes.append(UnlandedChange(
                sha=f"c0ffee{index:06d}", branch="fix/stranded-work", subject=f"stranded change {index}",
                committed_at=self.now - timedelta(days=30), lines_added=100, lines_present=2,
                files_measured=1, measurable=True, merged_into=("codex/parent",),
            ))
        return report

    def inventory(self, rows, receipts=(), landing=None):
        report = landing if landing is not None else LandingReport(repo="/owning-project", main_ref="origin/main")
        with patch.object(ui, "safe_runtime_processes", return_value=[]), patch.object(ui, "_agent_workspace_rows", return_value=[]), patch.object(ui, "recent_optimize_decisions", return_value=list(receipts)), patch.object(ui, "unlanded_work_for_project", return_value=report):
            return ui.build_optimize_inventory(rows)

    def test_parent_and_checkout_are_distinct_and_final_text_is_private(self):
        parent = self.session("parent", hours=0, final=False)
        child = self.session("child", {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})
        packet = session_review_identity(child, {"parent": parent})
        self.assertEqual(packet["session_role"], "subagent")
        self.assertEqual(packet["parent_session_id"], "parent")
        self.assertEqual(packet["parent_last_turn_status"], "started")
        self.assertEqual(packet["owning_cwd"], "/owning-project")
        self.assertEqual(packet["last_recorded_cwd"], "/another-checkout")
        self.assertEqual(packet["last_turn_status"], "completed")
        self.assertNotIn("PRIVATE FINAL TEXT", json.dumps(packet))

    def test_guardian_is_not_an_ordinary_stale_chat(self):
        rows = [self.session("guardian", {"subagent": {"other": "guardian"}}), self.session("reviewer", {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})]
        self.assertEqual(self.inventory(rows)["candidates"], [])

    def test_realistic_large_metadata_record_retains_role_without_private_content(self):
        row = self.session("guardian", {"subagent": {"other": "guardian"}})
        p = Path(row.source_path)
        records = p.read_text().splitlines()
        meta = json.loads(records[0])
        meta["payload"]["base_instructions"] = "PRIVATE INSTRUCTIONS " * 1400
        p.write_text(json.dumps(meta) + "\n" + "\n".join(records[1:]))
        identity = session_review_identity(row, {})
        self.assertEqual(identity["session_role"], "approval_session")
        self.assertNotIn("PRIVATE INSTRUCTIONS", json.dumps(identity))

    def test_latest_activity_includes_recent_session_and_useful_is_not_completion(self):
        rows = [self.session("old-a"), self.session("old-b"), self.session("current", hours=0, final=False)]
        item = self.inventory(rows, landing=self.landing_report())["top"]
        self.assertEqual(item["project_activity"]["session_id"], "current")
        self.assertIn("Recent activity", item["project_activity"]["status"])
        self.assertIn("latest user ask and final response", item["cleanup_prompt"])
        self.assertIn("confirmation question", item["cleanup_prompt"])
        self.assertIn("Immediately before any separately authorized", item["cleanup_prompt"])
        self.assertIn("Wait for explicit user confirmation of that exact final action summary", item["cleanup_prompt"])
        composed = ai_assist._structured_optimize_cleanup_text(
            {"next_verification": ["Confirm the latest owning-app status"]},
            local_prompt=item["cleanup_prompt"],
        )
        self.assertIn(item["cleanup_prompt"], composed)
        self.assertIn("Distinguish reversible archiving from permanent deletion", composed)
        self.assertNotIn("lower risk", item["cleanup_prompt"])

    def test_confirmation_instruction_changes_invalidate_cached_prompts(self):
        item = {"id": "candidate", "kind": "session_cluster"}
        before = ui._optimize_candidate_evidence_hash(item)
        with patch.object(ui, "_optimize_candidate_prompt", return_value="Updated final confirmation instructions"):
            self.assertNotEqual(before, ui._optimize_candidate_evidence_hash(item))

    def test_arbitrary_named_temporary_worktree_is_discovered_without_owner_in_scan(self):
        root = Path(self.tmp.name)
        owner = root / 'owner'
        checkout = root / 'project-promotion-copy'
        owner.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(owner), *args], check=True, capture_output=True)
        git('init', '-b', 'main')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'baseline')
        git('worktree', 'add', '-b', 'codex/promotion', str(checkout))
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            found = ui._agent_workspace_rows(set())
        candidate = next(r for r in found if r['path'] == str(checkout.resolve()))
        self.assertEqual(candidate['source'], 'git_worktree')
        self.assertEqual(candidate['branch'], 'codex/promotion')
        self.assertEqual(Path(candidate['common_git_directory']), owner.resolve() / '.git')
        self.assertNotIn(str(owner.resolve()), [r['path'] for r in found])

    def test_nested_codex_container_resolves_to_exact_git_checkout(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            found = ui._agent_workspace_rows(set())
        paths = [row['path'] for row in found]
        self.assertIn(str(checkout.resolve()), paths)
        self.assertNotIn(str(checkout.parent.resolve()), paths)
        candidate = next(row for row in found if row['path'] == str(checkout.resolve()))
        self.assertEqual(candidate['source'], 'git_worktree')
        self.assertTrue(candidate['registered_worktree'])

    def test_clean_landed_worktree_has_measured_disk_evidence_and_no_small_nudge(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        with patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            inventory = ui.build_optimize_inventory([])
        item = inventory['top']
        self.assertEqual(item['safety_state'], 'ready')
        self.assertEqual(item['workspace_evidence']['git_status'], 'clean')
        self.assertEqual(item['workspace_evidence']['unlanded_commit_count'], 0)
        self.assertGreaterEqual(item['size_bytes'], 4096)
        self.assertIn('ready to review', inventory['impact_label'])
        self.assertFalse(inventory['notify_companion'])

    def test_actual_disk_threshold_triggers_companion_without_token_proxy(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        measured = {'bytes': ui.OPTIMIZE_DISK_NOTIFY_BYTES, 'complete': True, 'entries': 1, 'basis': 'exclusive_allocated'}
        with patch.object(ui, '_workspace_disk_usage', return_value=measured), patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            inventory = ui.build_optimize_inventory([])
        self.assertTrue(inventory['notify_companion'])
        self.assertEqual(inventory['ready_disk_bytes'], ui.OPTIMIZE_DISK_NOTIFY_BYTES)
        self.assertIn('500.0 MB', inventory['companion_summary'])

    def test_unknown_process_cwd_and_windows_runtime_coverage_fail_closed(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        process = SimpleNamespace(cwd=None, pid=42, stale_reasons=[], stale=False)
        with patch.object(ui, 'safe_runtime_processes', return_value=[process]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            item = ui.build_optimize_inventory([])['top']
        self.assertEqual(item['safety_state'], 'review')
        self.assertIn('could not be matched', item['safety_reason'])
        with patch.object(ui.os, 'name', 'nt'):
            self.assertFalse(ui._runtime_inventory_coverage_complete([]))

    def test_failed_process_inventory_never_allows_ready_cleanup(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        unavailable = RuntimeProcessInventory(coverage_complete=False)
        with patch.object(ui, 'safe_runtime_processes', return_value=unavailable), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            item = ui.build_optimize_inventory([])['top']
        self.assertEqual(item['safety_state'], 'review')
        self.assertFalse(item['workspace_evidence']['runtime_coverage_complete'])

    def test_recent_files_protect_workspace_even_when_linked_session_is_old(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        workspace['mtime'] = self.now
        old = LocalSession(session_id='old-workspace-session', tool='codex-cli', project_path=str(checkout), updated_at=self.now - timedelta(days=2))
        with patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            item = ui.build_optimize_inventory([old])['top']
        self.assertEqual(item['safety_state'], 'active')
        self.assertTrue(item['workspace_evidence']['recent_file_active'])

    def test_workspace_audit_uses_one_global_deadline_and_bounded_directory_walk(self):
        root = Path(self.tmp.name) / 'large'
        root.mkdir()
        for index in range(20):
            (root / f'{index}.txt').write_text('x')
        with patch.object(ui, 'WORKSPACE_SIZE_MAX_ENTRIES', 5):
            disk = ui._workspace_disk_usage(root)
        self.assertFalse(disk['complete'])
        self.assertLessEqual(disk['entries'], 6)

        workspaces = [
            {'path': str(root / f'checkout-{index}'), 'agent_owned': True, 'source': 'git_worktree',
             'registered_worktree': True, 'mtime': self.now - timedelta(days=2)}
            for index in range(3)
        ]
        deadlines = []
        clean = {'git_status': 'clean', 'dirty_entry_count': 0, 'untracked_entry_count': 0,
                 'main_ref': 'main', 'unlanded_commit_count': 0, 'branch': 'codex/test'}
        def disk_usage(_path, *, deadline=None):
            deadlines.append(deadline)
            return {'bytes': 1, 'complete': False, 'entries': 0}
        def git_evidence(_path, *, deadline=None):
            deadlines.append(deadline)
            return dict(clean)
        with patch.object(ui, '_workspace_disk_usage', side_effect=disk_usage), patch.object(ui, '_workspace_git_evidence', side_effect=git_evidence), patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=workspaces):
            ui.build_optimize_inventory([])
        self.assertEqual(len(deadlines), 6)
        self.assertEqual(len(set(deadlines)), 1)

        expired = time.monotonic() - 1
        with patch.object(ui, '_worktree_rows', return_value=[]) as worktrees, patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_scan_agent_workspace_root') as scanner:
            self.assertEqual(ui._agent_workspace_rows(set(), deadline=expired), [])
        worktrees.assert_called_once_with(set(), deadline=expired)
        scanner.assert_not_called()

    def test_allocated_size_excludes_hard_links_from_reclaim_claims(self):
        root = Path(self.tmp.name) / 'allocated'
        sibling = Path(self.tmp.name) / 'allocated-sibling'
        root.mkdir()
        sibling.mkdir()
        original = root / 'payload.bin'
        original.write_bytes(b'x' * 4096)
        if not hasattr(original.stat(), 'st_blocks'):
            self.skipTest('allocated block accounting is unavailable')
        os.link(original, sibling / 'payload-copy.bin')
        disk = ui._workspace_disk_usage(root)
        sibling_disk = ui._workspace_disk_usage(sibling)
        self.assertEqual(disk['basis'], 'exclusive_allocated')
        self.assertEqual(disk['bytes'], 0)
        self.assertEqual(disk['shared_bytes_excluded'], original.stat().st_blocks * 512)
        self.assertEqual(disk['bytes'] + sibling_disk['bytes'], 0)

    def test_relative_time_label_does_not_break_receipt_suppression_hash(self):
        item = {'id': 'worktree:/tmp/example', 'kind': 'worktree', 'last_activity': '2026-01-01T00:00:00+00:00',
                'updated_label': '5h ago', 'safety_state': 'ready', 'size_bytes': 12}
        before = ui._optimize_candidate_evidence_hash(item)
        item['updated_label'] = '6h ago'
        self.assertEqual(before, ui._optimize_candidate_evidence_hash(item))

    def test_dirty_unlanded_and_active_workspaces_are_never_ready(self):
        root, _owner, checkout = self.nested_codex_worktree()
        with patch.object(ui, '_agent_workspace_scan_roots', return_value=[root]), patch.object(ui, '_worktree_rows', return_value=[]):
            workspace = next(row for row in ui._agent_workspace_rows(set()) if row['path'] == str(checkout.resolve()))
        (checkout / 'local-only.txt').write_text('keep me\n')
        ui._WORKSPACE_SIZE_CACHE.clear()
        with patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            dirty = ui.build_optimize_inventory([])['top']
        self.assertEqual(dirty['safety_state'], 'preserve')
        self.assertGreater(dirty['workspace_evidence']['untracked_entry_count'], 0)

        (checkout / 'local-only.txt').unlink()
        (checkout / 'unique.txt').write_text('unique\n')
        self.git(checkout, 'add', 'unique.txt')
        self.git(checkout, 'commit', '-m', 'unique work')
        with patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            unlanded = ui.build_optimize_inventory([])['top']
        self.assertEqual(unlanded['safety_state'], 'preserve')
        self.assertEqual(unlanded['workspace_evidence']['unlanded_commit_count'], 1)

        process = SimpleNamespace(cwd=str(checkout), pid=42, stale_reasons=[], stale=False)
        with patch.object(ui, 'safe_runtime_processes', return_value=[process]), patch.object(ui, 'recent_optimize_decisions', return_value=[]), patch.object(ui, '_agent_workspace_rows', return_value=[workspace]):
            active_inventory = ui.build_optimize_inventory([])
        self.assertEqual(active_inventory['top']['safety_state'], 'active')
        self.assertEqual(active_inventory['status'], 'quiet')
        self.assertFalse(active_inventory['notify_companion'])

    def test_same_project_path_does_not_hide_stale_workspace_and_recent_raw_cwd_preserves_it(self):
        path = Path(self.tmp.name) / 'scratch'
        path.mkdir()
        workspace = {'path': str(path), 'agent_owned': True, 'source': 'git_worktree',
                     'branch': 'codex/review', 'mtime': self.now - timedelta(days=2)}
        old = LocalSession(session_id='old', tool='codex-cli', project_path=str(path), updated_at=self.now-timedelta(days=2))
        recent = LocalSession(session_id='recent', tool='codex-cli', project_path='/owner', raw_cwd=str(path), updated_at=self.now)
        with patch.object(ui, '_agent_workspace_rows', return_value=[workspace]), patch.object(ui, 'safe_runtime_processes', return_value=[]), patch.object(ui, 'recent_optimize_decisions', return_value=[]):
            inventory = ui.build_optimize_inventory([old])
            item = inventory['top']
            self.assertEqual(item['kind'], 'worktree')
            self.assertIn('not a complete machine inventory', item['cleanup_prompt'])
            self.assertIn('squash/rebase/cherry-pick', item['cleanup_prompt'])
            self.assertIn('After that confirmation, execute', item['cleanup_prompt'])
            composed = ai_assist._structured_optimize_cleanup_text({}, local_prompt=item['cleanup_prompt'])
            self.assertIn(item['cleanup_prompt'], composed)
            current = ui.build_optimize_inventory([old, recent])
            self.assertEqual(current['status'], 'quiet')
            self.assertEqual(current['top']['safety_state'], 'active')

    def test_symlinked_scratch_directory_is_not_followed(self):
        root = Path(self.tmp.name)
        target = root / 'target'
        target.mkdir()
        (root / 'codex-linked').symlink_to(target, target_is_directory=True)
        self.assertEqual(ui._scan_agent_workspace_root(root, set()), [])

    def test_large_group_is_a_bounded_explicit_review_sample(self):
        rows = [self.session(f"s-{i}") for i in range(20)]
        item = self.inventory(rows, landing=self.landing_report(changes=20))["top"]
        self.assertEqual(len(item["changes"]), REVIEW_BATCH_SIZE)
        self.assertEqual(item["omitted_change_count"], 20 - REVIEW_BATCH_SIZE)
        self.assertLess(len(item["cleanup_prompt"]), ai_assist.MAX_OPTIMIZE_CLEANUP_INPUT_CHARS)
        self.assertIn("Do not classify omitted sessions", item["cleanup_prompt"])

    def test_unknown_or_mismatched_identity_stays_unknown(self):
        row = self.session("one")
        Path(row.source_path).write_text(json.dumps({"type": "session_meta", "payload": {"id": "another", "source": "cli"}}))
        self.assertEqual(session_review_identity(row, {})["session_role"], "unknown")
        self.assertEqual(session_review_identity(row, {})["last_turn_status"], "unknown")

    def test_new_turn_invalidates_fingerprint_and_completion(self):
        row = self.session("one")
        before = session_review_identity(row, {})
        with Path(row.source_path).open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        after = session_review_identity(row, {})
        self.assertNotEqual(before, after)
        self.assertEqual(after["last_turn_status"], "started")
        self.assertFalse(after["final_response_observed"])

    def test_new_activity_in_old_cached_row_is_preserved_and_becomes_project_latest(self):
        revived = self.session("revived", hours=12)
        with Path(revived.source_path).open("a") as f:
            f.write(json.dumps({"type": "event_msg", "timestamp": self.now.isoformat(), "payload": {"type": "task_started"}}) + "\n")
        rows = [revived, self.session("older-a"), self.session("older-b")]
        item = self.inventory(rows, landing=self.landing_report())["top"]
        self.assertEqual(item["project_activity"]["session_id"], revived.session_id)
        self.assertEqual(item["project_activity"]["latest_session_evidence"]["last_turn_status"], "started")

    def test_receipt_quiets_only_unchanged_candidate_for_24_hours(self):
        rows = [self.session("one"), self.session("two")]
        landing = self.landing_report()
        item = self.inventory(rows, landing=landing)["top"]
        receipt = {"decision": "reviewed", "created_at": self.now.isoformat(), "evidence": {"candidate_id": item["id"], "evidence_hash": item["evidence_hash"]}}
        self.assertEqual(self.inventory(rows, [receipt], landing=landing)["candidates"], [])
        self.assertIsNotNone(self.inventory(rows + [self.session("new", hours=0)], [receipt], landing=self.landing_report(changes=2))["top"])
        receipt["created_at"] = (self.now - timedelta(hours=25)).isoformat()
        self.assertIsNotNone(self.inventory(rows, [receipt], landing=landing)["top"])

    def test_model_object_schema_is_rejected_and_safe_suggestions_never_classify(self):
        config = {"mode": "cloud", "enabled_workflows": ["optimize_cleanup"]}
        with patch.object(ai_assist, "build_ai_assist_status", return_value={"ready": True, "mode": "cloud"}), patch.object(ai_assist, "_call_configured_chat", return_value={"text": json.dumps({"safe_to_archive_or_review": [{"path": "/p", "reason": "old"}], "next_verification": ["Check the exact chat"]})}):
            with self.assertRaises(ai_assist.AiAssistRejected):
                ai_assist.compose_optimize_cleanup_prompt(config, local_prompt="complete guardrails")
        text = ai_assist._structured_optimize_cleanup_text({"safe_to_archive_or_review": ["Safe because two days old"], "next_verification": ["Check the exact chat"]}, local_prompt="complete guardrails")
        self.assertNotIn("Safe because", text)
        self.assertIn("complete guardrails", text)

    def test_oversized_review_is_not_silently_truncated(self):
        with self.assertRaises(ai_assist.AiAssistUnavailable):
            ai_assist.compose_optimize_cleanup_prompt({}, local_prompt="x" * (ai_assist.MAX_OPTIMIZE_CLEANUP_INPUT_CHARS + 1))

    def test_legacy_disk_cache_is_rejected_even_if_complete_and_recent(self):
        path = Path(self.tmp.name) / "summary.json"
        payload = {"generated_at": self.now.isoformat(), "summary_complete": True,
                   "cache_schema_version": 13, "optimize": {"candidates": [{"id": "legacy"}]}}
        with patch.object(ui, "_summary_cache_path", return_value=path):
            path.write_text(json.dumps(payload))
            self.assertIsNone(ui._read_summary_disk_cache(7))
            payload["cache_schema_version"] = ui.SUMMARY_CACHE_SCHEMA_VERSION
            path.write_text(json.dumps(payload))
            self.assertIsNone(ui._read_summary_disk_cache(7))

    def test_legacy_memory_candidate_starts_refresh_instead_of_becoming_ready(self):
        legacy = {"optimize": {"candidates": [{"id": "legacy", "cleanup_prompt": "old"}]}}
        with patch.dict(ui._SUMMARY_CACHE, {7: (time.monotonic(), legacy)}, clear=True), patch.object(ui, "_maybe_refresh_summary_cache", return_value=True) as refresh, patch.object(ui, "_mark_summary_cache", side_effect=lambda data, **flags: flags):
            result = ui.build_summary_cached(7)
        refresh.assert_called_once_with(7, force=True)
        self.assertEqual(result["status"], "refreshing")

    def test_ai_composition_is_paused_before_any_model_call_during_refresh(self):
        item = {"id": "candidate", "review_contract_version": ui.OPTIMIZE_REVIEW_CONTRACT_VERSION}
        summary = {"cache": {"refreshing": True}, "optimize": {"candidates": [item]}}
        with patch.object(ui, "build_summary_cached", return_value=summary), patch.object(ui, "compose_optimize_cleanup_prompt") as compose:
            result = ui.build_ai_assisted_optimize_cleanup_prompt("candidate")
        self.assertEqual(result["status"], "refreshing")
        compose.assert_not_called()
