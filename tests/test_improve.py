from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import request, error

from aiwatcher_cli import improve, local_state, ui
from aiwatcher_cli.scanner import LocalSession


class ImproveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": str(Path(self.temp.name) / "state.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = datetime.now(timezone.utc)
        self.rows = [LocalSession(session_id=f"s{i}", tool="claude-code", project_path=f"/work/{i}/same-name",
                                  started_at=self.now, updated_at=self.now, cost_usd=float(i)) for i in range(3)]

    def cards(self):
        cards = [{"id": "pace", "title": "Higher pace", "body": "Compared with prior windows."},
                 {"id": "outcome-review", "title": "Review outcomes", "body": "Sampled evidence."}]
        evidence = {row.session_id: SimpleNamespace(inferred_outcome="useful") for row in self.rows}
        return improve.attach_evidence(cards, self.rows, self.rows, evidence, days=7, now=self.now)

    def test_exact_session_identity_and_bounded_scope(self):
        cards = self.cards()
        self.assertEqual([item["session_id"] for item in cards[0]["evidence"]], ["s2", "s1", "s0"])
        self.assertEqual(cards[0]["evidence"][0]["project"], "/work/2/same-name")
        self.assertEqual(len(cards[0]["evidence_key"]), 64)
        self.assertIn("3 sampled", cards[1]["scope_note"])

    def test_feedback_reorders_only_matching_evidence_and_expires(self):
        cards = self.cards()
        self.assertEqual(improve.current_view(cards)[0]["id"], "outcome-review")
        local_state.record_improve_decision(cards[1]["evidence_key"], "later")
        local_state.record_improve_decision(cards[1]["evidence_key"], "reviewed")
        current = improve.current_view(cards)
        self.assertEqual(current[0]["id"], "pace")
        self.assertEqual(current[1]["feedback"], "later")
        self.assertEqual(improve.current_view(cards, now=self.now + timedelta(days=2))[0]["id"], "outcome-review")
        self.assertNotIn("feedback", cards[1])

    def test_outcome_confirmation_updates_cached_cards_without_rescan(self):
        cards = self.cards()
        local_state.record_outcome("s1", "useful")
        view = improve.current_view(cards)
        self.assertEqual(view[0]["evidence_total"], 2)
        self.assertNotIn("s1", [item["session_id"] for item in view[0]["evidence"]])
        local_state.record_outcome("s0", "useful")
        local_state.record_outcome("s2", "useful")
        self.assertEqual([item["id"] for item in improve.current_view(cards)], ["pace"])

    def test_feedback_storage_has_no_paths_prompts_or_free_text(self):
        card = self.cards()[0]
        local_state.record_improve_decision(card["evidence_key"], "helpful")
        record = local_state.recent_improve_decisions()[0]
        self.assertEqual(set(record), {"key", "decision", "created_at"})
        with self.assertRaises(ValueError):
            local_state.record_improve_decision(card["evidence_key"], "execute")
        with self.assertRaises(ValueError):
            local_state.record_improve_decision("/private/path", "later")

    def test_ai_packet_excludes_paths_ids_charts_and_transcripts(self):
        card = self.cards()[0]
        card.update(chart={"secret": "hidden"}, transcript="secret transcript")
        encoded = json.dumps(improve.ai_packet(card))
        for forbidden in ("/work/", "s2", "secret", "transcript", "chart"):
            self.assertNotIn(forbidden, encoded)

    def test_no_request_after_compaction_is_not_zero_context(self):
        with patch.object(local_state, "compaction_outcomes", return_value=[{"before": {"context": 1000}}]):
            result = improve.recent_results()[0]
        self.assertIn("Waiting", result["body"])
        self.assertNotIn("after: 0", result["body"])

    def test_copied_handoff_is_not_completed_or_saved(self):
        local_state.record_handoff_decision(session_id="s0", decision="copy_handoff", reason="test")
        result = improve.recent_results()[0]
        self.assertEqual(result["title"], "Fresh Start prepared")
        self.assertIn("does not prove", result["body"])

    def test_ai_uses_only_selected_packet_not_global_history(self):
        card = self.cards()[0]
        with patch.object(ui, "build_summary_cached", return_value={}), \
             patch.object(ui, "_ask_ai_evidence_packet", side_effect=AssertionError("Global scope")), \
             patch.object(ui, "_run_ai_assist_workflow", return_value={"text": "", "result": {"status": "skipped"}}) as run:
            result = ui.answer_ai_assisted_question("Explain", insight=card)
        self.assertEqual(result["answer"], card["body"])
        self.assertNotIn("/work/", run.call_args.kwargs["local_text"])

    def test_companion_receipt_changes_context_to_follow_up(self):
        card = {"id": "replayed-context", "session_id": "s0", "title": "Replay", "body": "Observed cost"}
        improve.attach_evidence([card], self.rows, self.rows, {}, days=7)
        local_state.record_handoff_decision(session_id="s0", decision="copy_handoff", reason="test")
        view = improve.current_view([card], state=local_state.improve_snapshot())
        self.assertTrue(view[0]["follow_up"])
        self.assertEqual(view[0]["action_label"], "Review context follow-up")

    def post(self, path, payload, origin=None):
        server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.UIHandler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        req = request.Request(f"http://127.0.0.1:{server.server_port}{path}",
                              data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            try:
                with request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except error.HTTPError as response:
                with response:
                    return response.code, json.loads(response.read())
        finally:
            thread.join(timeout=5)
            server.server_close()

    def test_routes_reject_cross_origin_stale_and_invalid_inputs(self):
        cards = self.cards()
        with patch.object(ui, "build_summary_cached", return_value={"insights": cards}):
            status, _ = self.post("/api/improve-decision", {"insight_key": cards[0]["evidence_key"], "decision": "later"}, "https://untrusted.example")
            self.assertEqual(status, 403)
            status, _ = self.post("/api/improve-decision", {"insight_key": "stale", "decision": "later"})
            self.assertEqual(status, 409)
            status, _ = self.post("/api/improve-decision", [])
            self.assertEqual(status, 400)
            status, _ = self.post("/api/improve-decision", {"insight_key": cards[0]["evidence_key"], "decision": "later"})
            self.assertEqual(status, 200)
            self.assertEqual(len(local_state.recent_improve_decisions()), 1)

    def test_scoped_ai_needs_confirmation_and_local_explanation_never_calls_model(self):
        card = self.cards()[0]
        payload = {"insight_key": card["evidence_key"], "question": "What next?"}
        with patch.object(ui, "build_summary_cached", return_value={"insights": [card]}), \
             patch.object(ui, "answer_ai_assisted_question", return_value={"answer": "Scoped explanation"}) as ai:
            status, body = self.post("/api/ask-aiwatcher", payload)
            self.assertEqual(status, 200)
            self.assertEqual(body["answer"], card["body"])
            ai.assert_not_called()
            status, _ = self.post("/api/ask-aiwatcher", {**payload, "ai_assist": True})
            self.assertEqual(status, 400)
            ai.assert_not_called()
            status, _ = self.post("/api/ask-aiwatcher", {**payload, "ai_assist": True, "confirmed": True})
            self.assertEqual(status, 200)
            ai.assert_called_once_with("What next?", days=7, insight=card)


if __name__ == "__main__":
    unittest.main()
