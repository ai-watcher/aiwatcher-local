import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error
from unittest.mock import patch

from aiwatcher_cli import ui
from aiwatcher_cli.local_state import recent_optimize_decisions
from aiwatcher_cli.optimize_review import session_review_identity
from aiwatcher_cli.scanner import LocalSession


class OptimizeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": self.tmp.name + "/state.json"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.item = {"id": "sessions:/test", "review_contract_version": 2, "kind": "session_cluster", "project_full": "/test", "session_count": 2}
        self.item["evidence_hash"] = ui._optimize_candidate_evidence_hash(self.item)
        self.summary = patch.object(ui, "build_summary_cached", return_value={"optimize": {"candidates": [self.item]}})
        self.summary.start()
        self.addCleanup(self.summary.stop)

    def post(self, route, payload, origin=None):
        server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.UIHandler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        req = request.Request(f"http://127.0.0.1:{server.server_address[1]}{route}", data=json.dumps(payload).encode(), headers=headers)
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except error.HTTPError as exc:
            return exc.code, json.load(exc)
        finally:
            thread.join(timeout=5)
            server.server_close()

    def test_review_requires_confirmation_and_only_saves_scoped_metadata(self):
        payload = {"candidate_id": self.item["id"], "evidence_hash": self.item["evidence_hash"], "decision": "reviewed"}
        self.assertEqual(self.post("/api/optimize-decision", payload)[0], 400)
        self.assertEqual(recent_optimize_decisions(), [])
        payload.update(confirmed=True, evidence={"transcript": "private text"})
        status, receipt = self.post("/api/optimize-decision", payload)
        self.assertEqual(status, 200)
        self.assertFalse(receipt["evidence"]["cleanup_performed"])
        self.assertEqual(receipt["evidence"]["candidate_id"], self.item["id"])
        self.assertNotIn("private text", json.dumps(receipt))
        self.assertTrue(ui.companion_skip_active(f"optimize_workspace:{self.item['id']}:{self.item['evidence_hash']}"))
        self.assertFalse(ui.companion_skip_active("optimize_workspace:/test"))

    def test_changed_evidence_cannot_be_confirmed(self):
        status, result = self.post("/api/optimize-decision", {"candidate_id": self.item["id"], "evidence_hash": "old-snapshot", "decision": "reviewed", "confirmed": True})
        self.assertEqual(status, 409)
        self.assertIn("Evidence changed", result["error"])
        self.assertEqual(recent_optimize_decisions(), [])

    def test_a_new_turn_after_preview_blocks_confirmation(self):
        path = Path(self.tmp.name) / "rollout-session.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session", "source": "cli"}}) + "\n")
        row = LocalSession(session_id="session", tool="codex-cli", source_path=str(path), updated_at=datetime.now(timezone.utc))
        self.item["sessions"] = [session_review_identity(row, {})]
        with patch.dict(ui._SESSION_INDEX, {row.session_id: row}):
            status, preview = self.post("/api/optimize-review", {"candidate_id": self.item["id"]})
            self.assertEqual(status, 200)
            with path.open("a") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
            status, _ = self.post("/api/optimize-decision", {"candidate_id": self.item["id"], "evidence_hash": preview["evidence_hash"], "decision": "reviewed", "confirmed": True})
            self.assertEqual(status, 409)
        self.assertEqual(recent_optimize_decisions(), [])

    def test_review_snapshot_and_decision_reject_cross_origin_requests(self):
        for route in ["/api/optimize-review", "/api/optimize-decision"]:
            self.assertEqual(self.post(route, {"candidate_id": self.item["id"]}, "https://example.com")[0], 403)

    def test_snapshot_handles_invalid_window_and_preserves_unknowns(self):
        status, item = self.post("/api/optimize-review", {"candidate_id": self.item["id"], "days": "invalid"})
        self.assertEqual(status, 200)
        self.assertEqual(item["id"], self.item["id"])

    def test_basic_copy_rebuilds_canonical_packet_instead_of_cached_prompt(self):
        self.item["cleanup_prompt"] = "old cached cleanup prompt"
        status, item = self.post("/api/optimize-review", {"candidate_id": self.item["id"]})
        self.assertEqual(status, 200)
        self.assertIn("Review packet", item["cleanup_prompt"])
        self.assertIn("Present the decision to the user", item["cleanup_prompt"])
        self.assertNotIn("old cached cleanup prompt", item["cleanup_prompt"])
        self.assertEqual(recent_optimize_decisions(), [])

    def test_refreshing_or_legacy_evidence_cannot_be_copied_or_confirmed(self):
        for extra in [{"cache": {"refreshing": True}}, {"summary_complete": False}, {"cache": {"error": "failed"}}]:
            with patch.object(ui, "build_summary_cached", return_value={"optimize": {"candidates": [self.item]}, **extra}):
                status, result = self.post("/api/optimize-review", {"candidate_id": self.item["id"]})
                self.assertEqual(status, 409)
                self.assertIn(result["status"], ["refreshing", "error"])
                status, _ = self.post("/api/optimize-decision", {"candidate_id": self.item["id"], "decision": "reviewed", "confirmed": True})
                self.assertEqual(status, 409)
        self.item.pop("review_contract_version")
        self.assertEqual(self.post("/api/optimize-review", {"candidate_id": self.item["id"]})[0], 409)
        self.assertEqual(recent_optimize_decisions(), [])

    def test_copy_all_refreshes_packets_without_recording_a_decision(self):
        self.item["cleanup_prompt"] = "old cached cleanup prompt"
        status, result = self.post("/api/optimize-review", {"all": True})
        self.assertEqual(status, 200)
        self.assertNotIn("old cached cleanup prompt", result["cleanup_prompt"])
        self.assertEqual(recent_optimize_decisions(), [])
