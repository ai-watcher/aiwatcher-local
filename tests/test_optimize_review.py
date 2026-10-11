import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiwatcher_cli import ai_assist, ui
from aiwatcher_cli.optimize_review import session_review_identity, REVIEW_BATCH_SIZE
from aiwatcher_cli.scanner import LocalSession


class OptimizeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(timezone.utc)

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

    def inventory(self, rows, receipts=()):
        with patch.object(ui, "safe_runtime_processes", return_value=[]), patch.object(ui, "_agent_workspace_rows", return_value=[]), patch.object(ui, "recent_optimize_decisions", return_value=list(receipts)):
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
        item = self.inventory(rows)["top"]
        self.assertEqual(item["project_activity"]["session_id"], "current")
        self.assertIn("Recent activity", item["project_activity"]["status"])
        self.assertEqual({r["session_id"] for r in item["sessions"]}, {"old-a", "old-b"})
        self.assertIn("latest user ask and final response", item["cleanup_prompt"])
        self.assertIn("confirmation question", item["cleanup_prompt"])
        self.assertNotIn("lower risk", item["cleanup_prompt"])

    def test_large_group_is_a_bounded_explicit_review_sample(self):
        item = self.inventory([self.session(f"s-{i}") for i in range(20)])["top"]
        self.assertEqual(len(item["sessions"]), REVIEW_BATCH_SIZE)
        self.assertEqual(item["omitted_session_count"], 20 - REVIEW_BATCH_SIZE)
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
        item = self.inventory(rows)["top"]
        self.assertEqual(item["project_activity"]["session_id"], revived.session_id)
        self.assertNotIn(revived.session_id, {row["session_id"] for row in item["sessions"]})
        self.assertEqual(item["project_activity"]["latest_session_evidence"]["last_turn_status"], "started")

    def test_receipt_quiets_only_unchanged_candidate_for_24_hours(self):
        rows = [self.session("one"), self.session("two")]
        item = self.inventory(rows)["top"]
        receipt = {"decision": "reviewed", "created_at": self.now.isoformat(), "evidence": {"candidate_id": item["id"], "evidence_hash": item["evidence_hash"]}}
        self.assertEqual(self.inventory(rows, [receipt])["candidates"], [])
        self.assertIsNotNone(self.inventory(rows + [self.session("new", hours=0)], [receipt])["top"])
        receipt["created_at"] = (self.now - timedelta(hours=25)).isoformat()
        self.assertIsNotNone(self.inventory(rows, [receipt])["top"])

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
        item = {"id": "candidate", "review_contract_version": 2}
        summary = {"cache": {"refreshing": True}, "optimize": {"candidates": [item]}}
        with patch.object(ui, "build_summary_cached", return_value=summary), patch.object(ui, "compose_optimize_cleanup_prompt") as compose:
            result = ui.build_ai_assisted_optimize_cleanup_prompt("candidate")
        self.assertEqual(result["status"], "refreshing")
        compose.assert_not_called()
