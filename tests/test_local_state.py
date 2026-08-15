from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import local_state


def _mp_record_intervention_worker(state_file: str, index: int) -> None:
    # Runs in a freshly spawned child process (not a thread), so it exercises
    # the cross-process file lock, not just _STATE_LOCK's in-process guard.
    os.environ["AIWATCHER_STATE_FILE"] = state_file
    local_state.record_intervention(
        tool="claude",
        cwd="/repo",
        risk="low",
        score=0,
        findings=[],
        original_prompt=f"prompt {index}",
        suggested_prompt=f"prompt {index}",
        decision="allowed_original",
        selected_prompt=None,
    )


class LocalStateTests(unittest.TestCase):
    def test_state_lock_is_only_ever_used_inside_locked_state(self) -> None:
        # _STATE_LOCK guards only in-process threads; a bare `with _STATE_LOCK:`
        # in a new record_*/recent_*/get_* function would compile and pass
        # every other test while silently reintroducing the cross-process
        # race _locked_state() exists to close. This asserts the only
        # reference to the raw lock left after removing _locked_state()'s own
        # body is its single definition line -- i.e. nothing else touches it
        # directly. If this fails, whatever new function added a second
        # reference should be using `with _locked_state():` instead.
        source = inspect.getsource(local_state)
        locked_state_source = inspect.getsource(local_state._locked_state)
        remainder = source.replace(locked_state_source, "", 1)
        self.assertEqual(
            remainder.count("_STATE_LOCK"), 1,
            "_STATE_LOCK must only be referenced in its own definition and inside "
            "_locked_state(); use `with _locked_state():` in any new function instead.",
        )

    def test_concurrent_decisions_do_not_clobber_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                import threading as threading_module

                def record(index: int) -> None:
                    local_state.record_decision("session-1", f"decision {index}")

                threads = [threading_module.Thread(target=record, args=(i,)) for i in range(20)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                stored = local_state.recent_decisions("session-1", limit=20)

        self.assertEqual(len(stored), 20)

    def test_handoff_decisions_store_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.record_handoff_decision(
                    session_id="session-1",
                    decision="new_chat",
                    reason="Critical context pressure.",
                    expected_saved_context_tokens=240_000,
                    action_channel="dashboard",
                )
                recent = local_state.recent_handoff_decisions()

        self.assertEqual(record["phase"], "control")
        self.assertEqual(record["intervention_type"], "handoff_bubble")
        self.assertEqual(record["receipt_kind"], "fresh_start")
        self.assertEqual(record["source_session_id"], "session-1")
        self.assertEqual(record["action_channel"], "dashboard")
        self.assertIsNone(record["next_session_id"])
        self.assertEqual(record["next_session_correlation"]["status"], "waiting")
        self.assertEqual(recent[0]["decision"], "new_chat")
        self.assertEqual(recent[0]["expected_saved_context_tokens"], 240_000)
        self.assertNotIn("prompt", json.dumps(recent).lower())

    def test_link_handoff_decision_next_session_keeps_source_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.record_handoff_decision(
                    session_id="source-session",
                    decision="new_chat",
                    reason="Critical context pressure.",
                )
                linked = local_state.link_handoff_decision_next_session(
                    record["id"],
                    next_session_id="next-session",
                    correlation={"status": "linked", "confidence": "high", "reason": "same project"},
                )
                recent = local_state.recent_handoff_decisions()

        self.assertTrue(linked)
        self.assertEqual(recent[0]["session_id"], "source-session")
        self.assertEqual(recent[0]["source_session_id"], "source-session")
        self.assertEqual(recent[0]["next_session_id"], "next-session")
        self.assertEqual(recent[0]["next_session_correlation"]["status"], "linked")

    def test_intervention_stores_hashes_not_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                intervention_id = local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="high",
                    score=8,
                    findings=["Broad scope"],
                    original_prompt="delete the secret file",
                    suggested_prompt="inspect the smallest relevant file first",
                    decision="suggested",
                    selected_prompt="inspect the smallest relevant file first",
                    estimated_impact={
                        "available": True,
                        "confidence": "medium",
                        "savings": {"tokens": [1000, 2000], "api_value_usd": [0.1, 0.2]},
                    },
                    selected_risk="low",
                    selected_score=1,
                )
                local_state.link_intervention_session(intervention_id, "session-1")
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        serialized = json.dumps(stored)
        self.assertNotIn("delete the secret file", serialized)
        self.assertNotIn("inspect the smallest relevant file first", serialized)
        record = stored["interventions"][0]
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["phase"], "plan")
        self.assertEqual(record["intervention_type"], "prompt_preflight")
        self.assertEqual(record["predicted_impact"]["savings"]["tokens"], [1000, 2000])
        self.assertEqual(record["selected_risk"], "low")
        self.assertEqual(record["risk_points_reduced"], 7)

    def test_record_intervention_accepts_session_id_at_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="low",
                    score=0,
                    findings=[],
                    original_prompt="prompt",
                    suggested_prompt="prompt",
                    decision="allowed_original",
                    selected_prompt=None,
                    session_id="sess-real-id",
                )
                stored = local_state.recent_interventions(limit=1)

        self.assertEqual(stored[0]["session_id"], "sess-real-id")

    def test_recent_command_decisions_filters_by_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_command_decision(
                    tool="claude", command="rm -rf /", pattern_id="rm_rf", reason="destructive", decision="block",
                )
                with local_state._locked_state():
                    data = local_state._load()
                    data["command_decisions"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
                    local_state._save(data)

                recent = local_state.recent_command_decisions(limit=20, days=7)
                unfiltered = local_state.recent_command_decisions(limit=20)

        self.assertEqual(recent, [])
        self.assertEqual(len(unfiltered), 1)

    def test_command_decision_redacts_secret_bearing_commands(self) -> None:
        command = (
            "PGPASSWORD=hunter2 psql "
            "postgres://admin:supersecret@prod-db.example.com/app "
            "--api-key sk-testsecret123456"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_command_decision(
                    tool="claude",
                    command=command,
                    pattern_id="prod-connection-string",
                    reason="prod connection",
                    decision="block",
                )
                stored = local_state.recent_command_decisions(limit=1)[0]

        self.assertTrue(stored["command_redacted"])
        self.assertEqual(stored["command_hash"], local_state.command_hash(command))
        self.assertIn("PGPASSWORD=[redacted]", stored["command"])
        self.assertIn("postgres://[redacted]@prod-db.example.com/app", stored["command"])
        self.assertIn("--api-key [redacted]", stored["command"])
        self.assertNotIn("hunter2", stored["command"])
        self.assertNotIn("supersecret", stored["command"])
        self.assertNotIn("sk-testsecret123456", stored["command"])

    def test_record_hook_event_stores_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_hook_event(
                    tool="claude",
                    cwd="/repo",
                    event="received",
                    prompt_found=True,
                    session_id="sess-real-id",
                )
                events = local_state.recent_hook_events(limit=1)

        self.assertEqual(events[0]["session_id"], "sess-real-id")

    def test_record_ui_server_round_trips_through_get(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_ui_server("127.0.0.1", 8799)
                server = local_state.get_ui_server()

        self.assertEqual(server["host"], "127.0.0.1")
        self.assertEqual(server["port"], 8799)

    def test_record_ui_server_overwrites_previous_port(self) -> None:
        """`aiwatcher ui` falls back to the next free port on each run --
        watch --notify's deep link must follow the latest bind, not a stale
        one from an earlier, now-stopped instance."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_ui_server("127.0.0.1", 8765)
                local_state.record_ui_server("127.0.0.1", 8799)
                server = local_state.get_ui_server()

        self.assertEqual(server["port"], 8799)

    def test_get_ui_server_returns_none_when_never_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertIsNone(local_state.get_ui_server())

    def test_get_ui_server_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertIsNone(local_state.get_ui_server())

    def test_record_watch_notification_round_trips_through_recent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_watch_notification(
                    session_id="sess-abc",
                    tool="claude-code",
                    action="create handoff capsule now",
                    reason="context critical",
                    sent=True,
                    detail="terminal-notifier",
                    url="http://127.0.0.1:8765/?session=sess-abc",
                )
                rows = local_state.recent_watch_notifications(limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "sess-abc")
        self.assertTrue(rows[0]["sent"])
        self.assertEqual(rows[0]["url"], "http://127.0.0.1:8765/?session=sess-abc")

    def test_recent_watch_notifications_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertEqual(local_state.recent_watch_notifications(), [])

    def test_ambient_intervention_is_shared_across_delivery_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1",
                    signal_kind="critical_context",
                    action="start fresh",
                    severity="critical",
                    session_stamp="2026-08-08T20:00:00+00:00",
                    reason="Context is critical.",
                    urls={"dashboard": "http://127.0.0.1:8765/?session=sess-1"},
                    expected_savings={"context_tokens": 180_000, "confidence": "measured"},
                )
                self.assertTrue(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                local_state.record_ambient_intervention_action(
                    record["fingerprint"], state="displayed", channel="overlay"
                )
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="notification"))
                rows = local_state.recent_ambient_interventions()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], record["id"])
        self.assertEqual(rows[0]["expected_savings"]["context_tokens"], 180_000)

    def test_ambient_intervention_waits_for_two_observations_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                first = local_state.upsert_ambient_intervention(
                    session_id="sess-1",
                    signal_kind="critical_context",
                    action="fresh_chat",
                    severity="critical",
                    session_stamp="stamp-1",
                    reason="Context pressure.",
                    required_observations=2,
                )
                self.assertFalse(
                    local_state.ambient_intervention_delivery_allowed(first["fingerprint"], channel="overlay")
                )
                second = local_state.upsert_ambient_intervention(
                    session_id="sess-1",
                    signal_kind="critical_context",
                    action="fresh_chat",
                    severity="critical",
                    session_stamp="stamp-1",
                    reason="Context pressure.",
                    required_observations=2,
                )

                self.assertEqual(second["observation_count"], 2)
                self.assertTrue(
                    local_state.ambient_intervention_delivery_allowed(second["fingerprint"], channel="overlay")
                )

    def test_ambient_visible_channel_can_realert_when_severity_worsens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1",
                    signal_kind="velocity",
                    action="narrow scope",
                    severity="warning",
                    session_stamp="stamp-1",
                    reason="Fast activity.",
                )
                local_state.record_ambient_intervention_action(
                    record["fingerprint"], state="displayed", channel="overlay"
                )
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="notification"))
                local_state.upsert_ambient_intervention(
                    session_id="sess-1",
                    signal_kind="velocity",
                    action="narrow scope",
                    severity="critical",
                    session_stamp="stamp-2",
                    reason="Fast activity worsened.",
                )

                self.assertTrue(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="notification"))

    def test_ambient_action_stays_quiet_until_severity_worsens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="velocity", action="narrow scope",
                    severity="warning", session_stamp="stamp-1", reason="Fast activity.",
                )
                local_state.record_ambient_intervention_action(record["fingerprint"], state="acted", detail="Focused brief copied")
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                refreshed = local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="velocity", action="narrow scope",
                    severity="warning", session_stamp="stamp-2", reason="Fast activity continued.",
                )
                self.assertEqual(refreshed["id"], record["id"])
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="velocity", action="narrow scope",
                    severity="critical", session_stamp="stamp-3", reason="Fast activity became critical.",
                )
                self.assertTrue(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))

    def test_late_delivery_events_do_not_clear_terminal_ambient_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="critical_context", action="start fresh",
                    severity="critical", session_stamp="stamp-1", reason="Context pressure.",
                )
                local_state.record_ambient_intervention_action(record["fingerprint"], state="acted", channel="overlay")
                local_state.record_ambient_intervention_action(record["fingerprint"], state="failed", channel="overlay")
                local_state.record_ambient_intervention_action(record["fingerprint"], state="displayed", channel="notification")
                rows = local_state.recent_ambient_interventions()

        self.assertEqual(rows[0]["state"], "acted")
        self.assertEqual(rows[0]["channels"]["overlay"]["state"], "failed")
        self.assertEqual(rows[0]["channels"]["notification"]["state"], "displayed")
        self.assertEqual(rows[0]["events"][-1]["state"], "displayed")

    def test_failed_ambient_delivery_retries_after_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="critical_context", action="start fresh",
                    severity="critical", session_stamp="stamp-1", reason="Context pressure.",
                )
                local_state.record_ambient_intervention_action(record["fingerprint"], state="failed", channel="overlay")
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                rows = local_state.recent_ambient_interventions()
                rows[0]["channels"]["overlay"]["updated_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=6)
                ).isoformat()
                with local_state._locked_state():
                    data = local_state._load()
                    data["ambient_interventions"] = list(reversed(rows))
                    local_state._save(data)

                self.assertTrue(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))

    def test_ambient_snooze_expires_without_new_session_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.upsert_ambient_intervention(
                    session_id="sess-1", signal_kind="loop", action="stop and inspect",
                    severity="warning", session_stamp="stamp-1", reason="Repeated action.",
                )
                future = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                local_state.record_ambient_intervention_action(record["fingerprint"], state="snoozed", snoozed_until=future)
                self.assertFalse(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))
                past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                local_state.record_ambient_intervention_action(record["fingerprint"], state="snoozed", snoozed_until=past)
                self.assertTrue(local_state.ambient_intervention_delivery_allowed(record["fingerprint"], channel="overlay"))

    def test_watcher_heartbeat_round_trips_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                before = local_state.get_watcher_status()
                local_state.record_watcher_heartbeat(
                    pid=12345,
                    mode="watch",
                    interval_seconds=60,
                    notify=True,
                    overlay=True,
                )
                after = local_state.get_watcher_status()

        self.assertFalse(before["running"])
        self.assertEqual(before["status"], "stopped")
        self.assertTrue(after["running"])
        self.assertEqual(after["pid"], 12345)
        self.assertEqual(after["mode"], "watch")
        self.assertTrue(after["notify"])
        self.assertTrue(after["overlay"])

    def test_get_watcher_status_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            status = local_state.get_watcher_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["status"], "unknown")

    def test_recent_decisions_and_outcome_fail_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertEqual(local_state.recent_decisions("sess-1"), [])
            self.assertIsNone(local_state.get_outcome("sess-1"))

    def test_issue_brief_token_degrades_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            token = local_state.issue_brief_token("handoff_capsule")
        self.assertTrue(token.startswith("unverified-"))

    def test_outcome_notification_dedup_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertFalse(local_state.has_sent_notification("sess-1:survival:7"))
                local_state.record_notification_sent("sess-1:survival:7")
                self.assertTrue(local_state.has_sent_notification("sess-1:survival:7"))
                # A different signal_key for the same session must not be conflated.
                self.assertFalse(local_state.has_sent_notification("sess-1:reprompt"))
                # Recording the same key twice must not duplicate it.
                local_state.record_notification_sent("sess-1:survival:7")
                with local_state._locked_state():
                    data = local_state._load()
                self.assertEqual(data["sent_notification_keys"].count("sess-1:survival:7"), 1)

    def test_has_sent_notification_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertFalse(local_state.has_sent_notification("sess-1:survival:7"))

    def test_get_baselines_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertEqual(local_state.get_baselines(), {})

    def test_recent_command_decisions_fails_soft_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertEqual(local_state.recent_command_decisions(), [])

    def test_command_allowlist_fails_closed_when_state_lock_is_unavailable(self) -> None:
        with patch.object(local_state, "_locked_state", side_effect=OSError("read-only state")):
            self.assertFalse(local_state.is_command_pattern_always_allowed("rm-rf"))

    def test_outcome_replaces_previous_value_for_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_outcome("session-1", "rework")
                local_state.record_outcome("session-1", "useful", "tests passed")
                outcome = local_state.get_outcome("session-1")
                counts = local_state.outcome_counts()

        self.assertEqual(outcome["outcome"], "useful")
        self.assertEqual(outcome["note"], "tests passed")
        self.assertEqual(counts["useful"], 1)
        self.assertEqual(counts["rework"], 0)

    def test_rejects_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                with self.assertRaises(ValueError):
                    local_state.record_outcome("session-1", "maybe")

    def test_existing_shared_parent_permissions_are_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(local_state.os, "chmod") as chmod,
            ):
                local_state.record_outcome("session-1", "useful")

        if os.name == "nt":
            chmod.assert_not_called()
        else:
            chmod.assert_called_once_with(local_state.Path(state_file), 0o600)

    def test_cross_process_lock_blocks_a_second_holder_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                lock_path = local_state._lock_path()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with open(lock_path, "a+b") as first:
                    if first.seek(0, os.SEEK_END) == 0:
                        first.write(b"\0")
                        first.flush()
                    local_state._acquire_file_lock(first)
                    try:
                        with open(lock_path, "a+b") as second:
                            with (
                                patch.object(local_state, "LOCK_TIMEOUT_SECONDS", 0.2),
                                patch.object(local_state, "LOCK_POLL_SECONDS", 0.02),
                            ):
                                with self.assertRaises(local_state.StateLockTimeout):
                                    local_state._acquire_file_lock(second)
                    finally:
                        local_state._release_file_lock(first)

                # Once released, a fresh acquire succeeds immediately.
                with open(lock_path, "a+b") as third:
                    local_state._acquire_file_lock(third)
                    local_state._release_file_lock(third)

    def test_concurrent_interventions_do_not_clobber_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                import threading as threading_module

                def record(index: int) -> None:
                    local_state.record_intervention(
                        tool="claude",
                        cwd="/repo",
                        risk="low",
                        score=0,
                        findings=[],
                        original_prompt=f"prompt {index}",
                        suggested_prompt=f"prompt {index}",
                        decision="allowed_original",
                        selected_prompt=None,
                    )

                threads = [threading_module.Thread(target=record, args=(i,)) for i in range(20)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        self.assertEqual(len(stored["interventions"]), 20)

    def test_multiprocess_record_intervention_does_not_drop_entries(self) -> None:
        # test_concurrent_interventions_do_not_clobber_each_other above only
        # covers threads within one process, which never exercises
        # _cross_process_lock -- every real AIWatcher invocation (hooks
        # especially) is a separate OS process. This spawns 8 real processes
        # to prove the file lock, not just _STATE_LOCK, prevents a dropped
        # write.
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            processes = [
                multiprocessing.Process(target=_mp_record_intervention_worker, args=(state_file, i))
                for i in range(8)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)

            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                data = local_state._load()

        self.assertEqual(len(data["interventions"]), 8)

    def test_active_prompt_gate_expires_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_active_prompt_gate(
                    gate_id="gate-1",
                    tool="claude",
                    cwd="/repo",
                    risk="high",
                    score=8,
                    url="http://127.0.0.1:9999/",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                    session_id="sess-1",
                )
                self.assertEqual(local_state.active_prompt_gate()["id"], "gate-1")
                local_state.clear_active_prompt_gate("other-gate")
                self.assertEqual(local_state.active_prompt_gate()["id"], "gate-1")
                local_state.clear_active_prompt_gate("gate-1")
                self.assertIsNone(local_state.active_prompt_gate())

                local_state.record_active_prompt_gate(
                    gate_id="gate-2",
                    tool="claude",
                    cwd="/repo",
                    risk="high",
                    score=8,
                    url="http://127.0.0.1:9999/",
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
                self.assertIsNone(local_state.active_prompt_gate())

    def test_stale_lockfile_left_behind_does_not_deadlock_future_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                lock_path = local_state._lock_path()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                # Simulate a lock file left behind by a process that
                # exited without an active hold on it. The OS releases the
                # advisory lock itself when the holding process's file
                # descriptor closes, so the lock *file's* mere existence on
                # disk must never be mistaken for an active hold.
                lock_path.write_bytes(b"\0")

                with patch.object(local_state, "LOCK_TIMEOUT_SECONDS", 0.5):
                    started = time.monotonic()
                    local_state.record_outcome("session-1", "useful")
                    elapsed = time.monotonic() - started

                outcome = local_state.get_outcome("session-1")

        self.assertLess(elapsed, 0.5)
        self.assertEqual(outcome["outcome"], "useful")

    def test_lock_timeout_is_an_os_error(self) -> None:
        # StateLockTimeout must subclass OSError so the existing
        # `except OSError: pass` in cli.py's hook write paths
        # (_record_hook_intervention / _record_hook_event) already skips
        # recording on a stuck lock instead of crashing or blocking the
        # user's prompt, with no cli.py changes needed.
        self.assertTrue(issubclass(local_state.StateLockTimeout, OSError))

    def test_truncated_json_quarantines_file_and_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                handle.write('{"version": 2, "interventions": [{"id": "x"')
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                data = local_state._load()

            self.assertEqual(data, local_state._empty_state())
            self.assertFalse(os.path.exists(state_file))
            backups = [
                name for name in os.listdir(temp_dir)
                if name.startswith("local-state.corrupt-") and name.endswith(".json")
            ]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(temp_dir, backups[0]), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"version": 2, "interventions": [{"id": "x"')

    def test_non_dict_json_quarantines_file_and_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                handle.write("[1, 2, 3]")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                data = local_state._load()

            self.assertEqual(data, local_state._empty_state())
            backups = [
                name for name in os.listdir(temp_dir)
                if name.startswith("local-state.corrupt-")
            ]
            self.assertEqual(len(backups), 1)

    def test_valid_json_creates_no_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_outcome("session-1", "useful")
                data = local_state._load()

            self.assertEqual(data["outcomes"][0]["session_id"], "session-1")
            backups = [
                name for name in os.listdir(temp_dir)
                if name.startswith("local-state.corrupt-")
            ]
            self.assertEqual(backups, [])

    def test_read_os_error_raises_and_leaves_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "local-state.json")
            original_bytes = b'{"version": 2, "interventions": [], "outcomes": [], "hook_events": []}'
            with open(state_file, "wb") as handle:
                handle.write(original_bytes)
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(local_state.Path, "open", side_effect=PermissionError("denied")),
            ):
                with self.assertRaises(local_state.StateReadError):
                    local_state._load()

            with open(state_file, "rb") as handle:
                self.assertEqual(handle.read(), original_bytes)

    def test_state_read_error_from_hook_write_path_does_not_raise(self) -> None:
        # Hook write paths (cli.py's _record_hook_intervention /
        # _record_hook_event) already wrap record_intervention() in a bare
        # `except OSError: pass` so a bad state file never blocks the user's
        # AI flow. StateReadError must subclass OSError so that existing
        # handling covers it without each call site needing a new except clause.
        self.assertTrue(issubclass(local_state.StateReadError, OSError))

    def test_hook_events_are_privacy_safe_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                for index in range(55):
                    local_state.record_hook_event(
                        tool="codex",
                        cwd="/repo",
                        event="received",
                        prompt_found=True,
                        risk="high",
                        score=8,
                        error="bind failed" if index == 54 else None,
                    )
                events = local_state.recent_hook_events(limit=60)
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["error"], "bind failed")
        self.assertNotIn("Refactor the entire codebase", json.dumps(stored))

    def test_evidence_snapshot_never_persists_commit_text(self) -> None:
        # build_outcome_evidence() now captures real commit subject/body text
        # (see outcome_evidence.py) for use in ephemeral, copy-once handoff
        # briefs. That text must never reach the persistent evidence_snapshot
        # store on disk -- this test simulates the real evidence.to_json()
        # shape and asserts the subject/body text is filtered out before
        # anything is written.
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.record_evidence_snapshot("session-1", {
                    "repo_root": "/Users/dev/secret-project",
                    "commits": [{
                        "sha": "abcdef1234567890",
                        "subject": "fix login bug for acme corp",
                        "body": "Session tokens were not being refreshed for the acme account.",
                        "committed_at": "2026-07-13T10:00:00Z",
                    }],
                    "changed_files": ["src/auth/secrets.py"],
                    "tests": [{"artifact": "test-results/auth.xml"}],
                    "inferred_outcome": "useful",
                    "confidence": "low",
                })
                snapshots = local_state.evidence_snapshots_for_sessions({"session-1"})
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        serialized = json.dumps(stored)
        self.assertEqual(record["commit_shas"], ["abcdef123456"])
        self.assertEqual(snapshots["session-1"]["inferred_outcome"], "useful")
        self.assertNotIn("/Users/dev/secret-project", serialized)
        self.assertNotIn("src/auth/secrets.py", serialized)
        self.assertNotIn("test-results/auth.xml", serialized)
        self.assertNotIn("fix login bug", serialized)
        self.assertNotIn("acme", serialized)

    def test_evidence_plaintext_never_leaks_into_any_other_stored_record(self) -> None:
        # S-22 audit follow-up: test_evidence_snapshot_never_persists_commit_text
        # above only guards record_evidence_snapshot()'s own filtering. This
        # guards the broader claim -- that the same session's plaintext
        # commit/file evidence (real text, by design -- see outcome_evidence.py's
        # _recent_commits docstring) never ends up in *any* other persisted
        # table (interventions, hook_events) just because those records were
        # written around the same time for the same session.
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_evidence_snapshot("session-1", {
                    "repo_root": "/Users/dev/secret-project",
                    "commits": [{
                        "sha": "abcdef1234567890",
                        "subject": "fix login bug for acme corp",
                        "body": "Session tokens were not being refreshed for the acme account.",
                        "committed_at": "2026-07-13T10:00:00Z",
                    }],
                    "changed_files": ["src/auth/secrets.py"],
                    "tests": [{"artifact": "test-results/auth.xml"}],
                    "inferred_outcome": "useful",
                    "confidence": "low",
                })
                # cwd is intentionally stored in plaintext by these two (unrelated,
                # pre-existing design choice -- see the cwd= fields in record_intervention/
                # record_hook_event) -- use a *different* path than evidence's repo_root
                # so this test isolates evidence-specific leakage, not that separate behavior.
                local_state.record_intervention(
                    tool="claude", cwd="/repo", risk="low", score=0,
                    findings=[], original_prompt="prompt", suggested_prompt="prompt",
                    decision="allowed_original", selected_prompt=None, session_id="session-1",
                )
                local_state.record_hook_event(
                    tool="claude", cwd="/repo", event="received",
                    prompt_found=True, session_id="session-1",
                )
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        serialized = json.dumps(stored)
        self.assertNotIn("/Users/dev/secret-project", serialized)
        self.assertNotIn("src/auth/secrets.py", serialized)
        self.assertNotIn("test-results/auth.xml", serialized)
        self.assertNotIn("fix login bug", serialized)
        self.assertNotIn("acme", serialized)

    def test_record_survival_check_attaches_to_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_evidence_snapshot("session-1", {"commits": [{"sha": "abc123"}]})
                attached = local_state.record_survival_check("session-1", "7", "survived")
                snapshots = local_state.evidence_snapshots_for_sessions({"session-1"})

        self.assertTrue(attached)
        self.assertEqual(snapshots["session-1"]["survival"]["7"]["status"], "survived")
        self.assertIn("checked_at", snapshots["session-1"]["survival"]["7"])

    def test_record_survival_check_returns_false_without_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                attached = local_state.record_survival_check("no-such-session", "7", "survived")
        self.assertFalse(attached)

    def test_record_survival_check_rejects_invalid_bucket_or_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_evidence_snapshot("session-1", {"commits": [{"sha": "abc123"}]})
                with self.assertRaises(ValueError):
                    local_state.record_survival_check("session-1", "8", "survived")
                with self.assertRaises(ValueError):
                    local_state.record_survival_check("session-1", "7", "maybe")

    def test_resnapshotting_a_session_preserves_recorded_at_and_survival(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                first = local_state.record_evidence_snapshot("session-1", {"commits": [{"sha": "abc123"}]})
                local_state.record_survival_check("session-1", "7", "churned")
                # Re-opening the session in the dashboard later re-records the snapshot.
                second = local_state.record_evidence_snapshot("session-1", {"commits": [{"sha": "abc123"}]})
                snapshots = local_state.evidence_snapshots_for_sessions({"session-1"})

        self.assertEqual(second["recorded_at"], first["recorded_at"])
        self.assertEqual(snapshots["session-1"]["survival"]["7"]["status"], "churned")

    def test_record_decision_round_trips_and_orders_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_decision(
                    "session-1",
                    "Chose real commit subject/body over hashing",
                    reasoning="A commit message explains itself to a future reader.",
                    alternatives_rejected=["hashing the subject"],
                )
                local_state.record_decision(
                    "session-1",
                    "Considered a token-based tiebreaker",
                    reasoning="Still picks turn #1 for unrelated reasons.",
                    alternatives_rejected=["token-based tiebreaker", "git diff --stat only"],
                )
                local_state.record_decision("session-2", "Unrelated decision for a different session")
                decisions = local_state.recent_decisions("session-1")

        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["summary"], "Considered a token-based tiebreaker")
        self.assertEqual(decisions[0]["alternatives_rejected"], ["token-based tiebreaker", "git diff --stat only"])
        self.assertEqual(decisions[1]["summary"], "Chose real commit subject/body over hashing")

    def test_record_decision_requires_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                with self.assertRaises(ValueError):
                    local_state.record_decision("session-1", "   ")

    def test_recent_decisions_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                for i in range(8):
                    local_state.record_decision("session-1", f"decision {i}")
                decisions = local_state.recent_decisions("session-1", limit=5)

        self.assertEqual(len(decisions), 5)
        self.assertEqual(decisions[0]["summary"], "decision 7")


if __name__ == "__main__":
    unittest.main()
