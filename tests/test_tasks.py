from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from aiwatcher_cli import scanner, tasks
from aiwatcher_cli.scanner import LocalSession


T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def seg(prompt: str, *, minutes: int, tokens: int = 1000, cost: float = 0.1, calls: int = 1) -> dict:
    return {
        "prompt": prompt,
        "at": (T0 + timedelta(minutes=minutes)).isoformat(),
        "tokens": tokens,
        "cost_usd": cost,
        "tool_calls": calls,
        "events": 1,
    }


def numbered(segments: list[dict]) -> list[dict]:
    for index, segment in enumerate(segments):
        segment["turn"] = index + 1
    return segments


def session(session_id: str = "sess-1", *, started: datetime = T0, updated: datetime | None = None) -> LocalSession:
    return LocalSession(
        session_id=session_id,
        tool="claude-code",
        project_path="/tmp/repo",
        started_at=started,
        updated_at=updated or (started + timedelta(hours=1)),
        source_path=f"/tmp/{session_id}.jsonl",
    )


class BoundaryRuleTests(unittest.TestCase):
    def test_first_prompt_always_opens_a_task(self) -> None:
        self.assertEqual(tasks.detect_boundary("yes", index=0, previous_references=set(), gap=None), (True, "session_start", "high"))

    def test_short_reactions_never_open_a_task(self) -> None:
        for text in ("yes", "go ahead", "ok do it", "no"):
            boundary, _, _ = tasks.detect_boundary(text, index=3, previous_references=set(), gap=None)
            self.assertFalse(boundary, text)

    def test_instruction_naming_new_thing_is_a_confident_boundary(self) -> None:
        boundary, method, confidence = tasks.detect_boundary(
            "now review PR #98 and post the findings", index=4, previous_references={"scanner.py"}, gap=None
        )
        self.assertEqual((boundary, method, confidence), (True, "rules", "high"))

    def test_follow_up_that_leans_on_the_last_answer_is_not_a_boundary(self) -> None:
        boundary, _, _ = tasks.detect_boundary(
            "can you make that the same colour as the other one", index=2, previous_references=set(), gap=None
        )
        self.assertFalse(boundary)

    def test_instruction_with_no_reference_is_a_medium_boundary(self) -> None:
        boundary, _, confidence = tasks.detect_boundary("let's build the mockup", index=2, previous_references=set(), gap=None)
        self.assertEqual((boundary, confidence), (True, "medium"))

    def test_long_pause_then_fresh_prompt_is_a_boundary(self) -> None:
        boundary, _, confidence = tasks.detect_boundary(
            "the dashboard numbers look wrong on the sessions page", index=5, previous_references=set(), gap=timedelta(hours=8)
        )
        self.assertEqual((boundary, confidence), (True, "medium"))

    def test_label_trims_to_a_few_words_and_names_attachments(self) -> None:
        self.assertEqual(tasks.label_for("Help me review this PR raised by my partner please, thanks"), "Help me review this PR raised by my…")
        self.assertEqual(tasks.label_for('@"/Users/me/Downloads/spike2.html"\n\nlook at this'), "Attached spike2.html")


class SessionTaskTests(unittest.TestCase):
    def segments(self) -> list[dict]:
        return numbered([
            seg("Help me review PR #98", minutes=0, tokens=100),
            seg("first tell me what it changes", minutes=3, tokens=200),
            seg("ok post the findings", minutes=9, tokens=50),
            seg("now fix the pricing table in pricing.py", minutes=15, tokens=400, calls=5),
            seg("yes", minutes=16, tokens=25),
        ])

    def test_rules_split_the_session_into_two_tasks(self) -> None:
        result = tasks.build_session_tasks(session(), self.segments(), now=T0 + timedelta(days=1))
        self.assertEqual([task["label"] for task in result], ["Help me review PR #98", "now fix the pricing table in pricing.py"])
        self.assertEqual([task["turns"] for task in result], [3, 2])
        self.assertEqual(result[0]["tokens"], 350)
        self.assertEqual(result[1]["tool_calls"], 6)
        self.assertEqual(result[0]["ended_at"], result[1]["started_at"])
        self.assertEqual(result[0]["id"], tasks.task_id_for("sess-1", 1))
        self.assertEqual(result[1]["start_turn"], 4)
        self.assertEqual(result[0]["status"], "ended")

    def test_user_override_beats_the_rules_both_ways(self) -> None:
        # Merge: suppress the boundary the rules found at turn 4. Split: force one at turn 2.
        result = tasks.build_session_tasks(session(), self.segments(), overrides={4: False, 2: True})
        self.assertEqual([task["start_turn"] for task in result], [1, 2])
        self.assertEqual([task["boundary_method"] for task in result], ["session_start", "user"])
        self.assertEqual(result[1]["confidence"], "confirmed")
        self.assertEqual(result[1]["turns"], 4)
        # The merged-away boundary at turn 4 sits inside the second task, so that
        # task is marked corrected even though its own start was a user split.
        self.assertTrue(result[1]["corrected"])
        self.assertFalse(result[0]["corrected"])

    def test_a_merge_marks_the_task_that_absorbed_it(self) -> None:
        result = tasks.build_session_tasks(session(), self.segments(), overrides={4: False})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["boundary_method"], "session_start")
        self.assertTrue(result[0]["corrected"])

    def test_last_task_of_a_live_session_is_open(self) -> None:
        live = session(updated=T0 + timedelta(minutes=20))
        result = tasks.build_session_tasks(live, self.segments(), now=T0 + timedelta(minutes=25))
        self.assertEqual(result[-1]["status"], "open")
        self.assertEqual(result[0]["status"], "ended")


class LinkingTests(unittest.TestCase):
    def build(self) -> list[dict]:
        segments = numbered([
            seg("Help me review PR #98", minutes=0),
            seg("now fix the pricing table in pricing.py", minutes=30),
        ])
        return tasks.build_session_tasks(session(), segments, now=T0 + timedelta(days=1))

    def test_commit_lands_on_the_task_open_when_it_was_authored(self) -> None:
        result = self.build()
        change = SimpleNamespace(sha="abc1234def", subject="fix(pricing): rates", repo="/tmp/repo",
                                 landed_at=T0 + timedelta(minutes=45), session_ids=["sess-1"])
        tasks.attach_commits(result, [change], alias={})
        self.assertEqual(result[0]["commits"], [])
        self.assertEqual(result[1]["commits"][0]["sha"], "abc1234def")

    def test_commit_recorded_against_a_forked_session_id_still_lands(self) -> None:
        result = self.build()
        change = SimpleNamespace(sha="beef", subject="s", repo="r", landed_at=T0 + timedelta(minutes=5), session_ids=["fork-9"])
        tasks.attach_commits(result, [change], alias={"fork-9": "sess-1"})
        self.assertEqual(result[0]["commits"][0]["sha"], "beef")

    def test_only_applied_briefs_and_taken_fresh_starts_count(self) -> None:
        result = self.build()
        interventions = [
            {"decision": "allowed_original", "session_id": "sess-1", "created_at": (T0 + timedelta(minutes=1)).isoformat()},
            {"decision": "brief_accepted", "session_id": "sess-1", "created_at": (T0 + timedelta(minutes=29)).isoformat(),
             "score": 19, "selected_score": 2},
        ]
        handoffs = [
            {"decision": "dismissed", "source_session_id": "sess-1", "created_at": (T0 + timedelta(minutes=40)).isoformat()},
            {"decision": "copy_handoff", "source_session_id": "sess-1", "created_at": (T0 + timedelta(minutes=50)).isoformat(),
             "next_session_correlation": {"status": "waiting"}},
        ]
        tasks.attach_interventions(result, interventions, handoffs, alias={})
        self.assertEqual(result[0]["interventions"], [])
        kinds = [(row["kind"], row["decision"]) for row in result[1]["interventions"]]
        self.assertEqual(kinds, [("prompt_brief", "brief_accepted"), ("fresh_start", "copy_handoff")])
        self.assertEqual(result[1]["interventions"][1]["link_status"], "waiting")


class BuildTasksTests(unittest.TestCase):
    def _transcript(self, directory: Path, name: str, prompts: list[tuple[str, int]]) -> Path:
        rows = []
        for index, (prompt, minute) in enumerate(prompts):
            stamp = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
            rows.append({"type": "user", "timestamp": stamp, "cwd": "/tmp/repo",
                         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
            rows.append({"type": "assistant", "timestamp": stamp, "cwd": "/tmp/repo", "requestId": f"req-{name}-{index}",
                         "message": {"role": "assistant", "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "ok"}],
                                     "usage": {"input_tokens": 100, "output_tokens": 10}}})
        if name == "titled":
            rows.append({"type": "custom-title", "customTitle": "Pricing table fix", "sessionId": name})
        path = directory / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_builds_tasks_across_sessions_and_reports_unmeasurable_ones(self) -> None:
        scanner.SEGMENT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            titled = self._transcript(directory, "titled", [("fix the pricing table", 0), ("yes", 2), ("now add tests for pricing.py", 20)])
            rows = [
                LocalSession(session_id="titled", tool="claude-code", project_path="/tmp/repo", started_at=T0,
                             updated_at=T0 + timedelta(minutes=30), source_path=str(titled)),
                LocalSession(session_id="codex-db", tool="codex-cli", project_path="/tmp/repo", started_at=T0,
                             updated_at=T0, source_path="/tmp/state_5.sqlite"),
            ]
            result = tasks.build_tasks(rows, now=T0 + timedelta(days=1))
        self.assertEqual(result["session_count"], 1)
        self.assertEqual([task["label"] for task in result["tasks"]], ["fix the pricing table", "now add tests for pricing.py"])
        self.assertEqual(result["tasks"][0]["session_title"], "Pricing table fix")
        self.assertEqual(result["unmeasurable"][0]["session_id"], "codex-db")
        self.assertFalse(result["sized"], "two tasks is too few to claim size buckets")

    def test_forked_desktop_copy_is_folded_into_one_session(self) -> None:
        scanner.SEGMENT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            prompts = [("build the mockup", 0), ("ok", 1)]
            original = self._transcript(directory, "orig", prompts)
            fork = self._transcript(directory, "fork", prompts)
            rows = [
                LocalSession(session_id="orig", tool="claude-code", project_path="/tmp/repo", started_at=T0,
                             updated_at=T0 + timedelta(hours=2), source_path=str(original)),
                LocalSession(session_id="fork", tool="claude-code", project_path="/tmp/repo", started_at=T0,
                             updated_at=T0 + timedelta(hours=1), source_path=str(fork)),
            ]
            change = SimpleNamespace(sha="c0ffee", subject="s", repo="/tmp/repo", landed_at=T0 + timedelta(minutes=5), session_ids=["fork"])
            result = tasks.build_tasks(rows, changes=[change], now=T0 + timedelta(days=1))
        self.assertEqual(result["twin_sessions_folded"], 1)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["session_id"], "orig")
        self.assertEqual(result["tasks"][0]["commits"][0]["sha"], "c0ffee")


if __name__ == "__main__":
    unittest.main()
