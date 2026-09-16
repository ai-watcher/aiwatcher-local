from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli, compaction_outcomes, local_state, ui
from aiwatcher_cli.scanner import LocalSession

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class Transcript:
    """A Claude Code transcript, written the way Claude Code writes it: compact
    JSON (the compaction marker is matched in raw bytes), one row per line."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._n = 0

    def _uuid(self) -> str:
        self._n += 1
        return f"row-{self._n}"

    def prompt(self, minutes: float, text: str = "do the next thing") -> None:
        self.rows.append({
            "type": "user", "uuid": self._uuid(), "timestamp": at(minutes),
            "message": {"role": "user", "content": text},
        })

    def request(
        self, minutes: float, *, read: int = 0, write_1h: int = 0, fresh: int = 10, out: int = 200,
        request_id: str | None = None, tools: tuple[tuple[str, str, str], ...] = (), model: str = "claude-opus-5",
    ) -> None:
        self.rows.append({
            "type": "assistant", "uuid": self._uuid(), "requestId": request_id or f"req-{self._n}",
            "timestamp": at(minutes),
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": fresh, "output_tokens": out, "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": write_1h,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write_1h},
                },
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": {"file_path": path}}
                    for tool_id, name, path in tools
                ],
            },
        })

    def result(self, minutes: float, tool_id: str, text: str) -> None:
        self.rows.append({
            "type": "user", "uuid": self._uuid(), "timestamp": at(minutes),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
        })

    def compact(self, minutes: float, *, pre: int, trigger: str = "manual", summary: str = "The summary. " * 50) -> None:
        self.rows.append({
            "type": "system", "subtype": "compact_boundary", "uuid": self._uuid(), "timestamp": at(minutes),
            "content": "Conversation compacted",
            "compactMetadata": {"trigger": trigger, "preTokens": pre, "durationMs": 60_000},
        })
        self.rows.append({
            "type": "user", "uuid": self._uuid(), "timestamp": at(minutes), "isCompactSummary": True,
            "message": {"role": "user", "content": summary},
        })

    def command(self, minutes: float) -> None:
        self.rows.append({
            "type": "user", "uuid": self._uuid(), "timestamp": at(minutes),
            "message": {"role": "user", "content": "<command-name>/compact</command-name>\n<command-message>compact</command-message>"},
        })

    def recopy(self, index: int) -> None:
        """What Claude Code does at compaction: an earlier row written again,
        same uuid and timestamp, after the live tail."""
        self.rows.append(dict(self.rows[index]))

    def write(self, path: Path) -> str:
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in self.rows), encoding="utf-8")
        return str(path)


def one_compaction() -> Transcript:
    transcript = Transcript()
    transcript.prompt(0)
    transcript.request(1, write_1h=40_000)                 # 40,010
    transcript.prompt(2)
    transcript.request(3, read=40_000, write_1h=260_000)   # 300,010
    transcript.compact(5, pre=300_010)
    transcript.prompt(6)
    transcript.request(7, read=20_000, write_1h=40_000)    # 60,010: the rebuild
    transcript.request(8, read=60_000, write_1h=2_000)     # 62,010
    return transcript


class MeasureTranscriptTests(unittest.TestCase):
    """Each record is read from the transcript: the request either side of the
    compaction and the stretch of typed prompts after it."""

    def measure(self, transcript: Transcript) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            path = transcript.write(Path(tmp, "s1.jsonl"))
            return compaction_outcomes.measure_transcript(path, session_id="s1")

    def test_a_compaction_is_measured_from_the_requests_either_side(self) -> None:
        (record,) = self.measure(one_compaction())
        self.assertEqual(record["trigger"], "manual")
        self.assertEqual(record["pre_tokens"], 300_010)
        self.assertEqual(record["before"]["context"], 300_010)
        self.assertEqual(record["first_after"]["context"], 60_010)
        self.assertEqual(record["first_after"]["cache_write_1h"], 40_000)
        self.assertEqual(record["gap_seconds"], 240)
        # The session wrote 1h cache entries, so a four-minute pause kept it warm.
        self.assertEqual(record["cache_ttl_seconds"], compaction_outcomes.TTL_1H_SECONDS)
        self.assertEqual(record["after"]["prompts"], 1)
        self.assertEqual(record["after"]["requests"], 2)
        self.assertEqual(record["after"]["rebuilds_1h"], 0)
        self.assertEqual(record["summary_chars"], len("The summary. " * 50))
        self.assertIsNone(record["closed_by"])
        self.assertEqual(record["id"], "s1:row-5")

    def test_rows_written_again_and_block_copies_count_once(self) -> None:
        transcript = one_compaction()
        # A second content-block line of the 300k request (same requestId) and
        # a copy of the 40k row re-appended right before the boundary: the
        # size before is still the last real request, not the copy.
        transcript.rows.insert(4, {**transcript.rows[3], "uuid": "block-2"})
        transcript.rows.insert(5, dict(transcript.rows[1]))
        (record,) = self.measure(transcript)
        self.assertEqual(record["before"]["context"], 300_010)
        self.assertEqual(record["before"]["requests"], 2)

    def test_the_stretch_closes_on_the_prompt_after_the_tenth(self) -> None:
        transcript = one_compaction()
        for index in range(11):
            transcript.prompt(10 + index)
            transcript.request(10.5 + index, read=62_000, write_1h=500)
        (record,) = self.measure(transcript)
        self.assertEqual(record["after"]["prompts"], 10)
        # The two requests right after the compaction, plus one per prompt up
        # to the tenth; the eleventh prompt's request is outside.
        self.assertEqual(record["after"]["requests"], 2 + 9)
        self.assertEqual(record["closed_by"], "prompts")

    def test_the_typed_command_is_found_after_the_boundary_with_its_earlier_time(self) -> None:
        transcript = Transcript()
        transcript.prompt(0)
        transcript.request(1, write_1h=300_000)
        transcript.compact(5, pre=300_010)
        transcript.command(4)        # written once compaction finished, stamped when typed
        transcript.prompt(6)
        transcript.request(7, read=20_000, write_1h=40_000)
        (record,) = self.measure(transcript)
        self.assertEqual(record["command_at"], (T0 + timedelta(minutes=4)).isoformat())
        self.assertEqual(record["after"]["prompts"], 1)

    def test_a_compaction_with_no_typed_command_has_no_command_time(self) -> None:
        transcript = one_compaction()
        (record,) = self.measure(transcript)
        self.assertIsNone(record["command_at"])

    def test_a_second_compaction_cuts_the_stretch_short(self) -> None:
        transcript = one_compaction()
        transcript.prompt(9)
        transcript.request(10, read=62_000, write_1h=40_000)
        transcript.compact(12, pre=102_010)
        transcript.prompt(13)
        transcript.request(14, read=20_000, write_1h=30_000)
        first, second = self.measure(transcript)
        self.assertEqual(first["closed_by"], "next_compaction")
        self.assertEqual(first["after"]["prompts"], 2)
        self.assertEqual(second["before"]["context"], 102_010)
        self.assertIsNone(second["closed_by"])

    def test_a_later_rebuild_is_counted_but_the_first_request_after_is_not(self) -> None:
        transcript = one_compaction()
        transcript.prompt(90)
        transcript.request(91, read=10_000, write_1h=55_000)   # the cache expired over lunch
        (record,) = self.measure(transcript)
        self.assertEqual(record["after"]["rebuilds_1h"], 1)

    def test_rereads_count_only_files_the_removed_history_touched(self) -> None:
        transcript = Transcript()
        transcript.prompt(0)
        transcript.request(1, write_1h=50_000, tools=(("t1", "Read", "/r/a.py"), ("t2", "Edit", "/r/b.py")))
        transcript.result(1.5, "t1", "x" * 1000)
        transcript.compact(3, pre=50_010)
        transcript.prompt(4)
        transcript.request(5, read=20_000, write_1h=5_000, tools=(
            ("t3", "Read", "/r/a.py"), ("t4", "Read", "/r/b.py"), ("t5", "Read", "/r/c.py"),
        ))
        transcript.result(5.5, "t3", "x" * 4000)
        transcript.result(5.5, "t4", "y" * 400)
        transcript.result(5.5, "t5", "z" * 8000)
        (record,) = self.measure(transcript)
        self.assertEqual(record["after"]["reads"], 3)
        self.assertEqual(record["after"]["rereads"], 2)
        self.assertEqual(record["after"]["reread_chars"], 4400)

    def test_the_record_holds_no_text_and_no_paths(self) -> None:
        transcript = Transcript()
        transcript.prompt(0, text="PRIVATE PROMPT TEXT")
        transcript.request(1, write_1h=50_000, tools=(("t1", "Read", "/secret/place/a.py"),))
        transcript.result(1.5, "t1", "PRIVATE FILE CONTENTS")
        transcript.compact(3, pre=50_010, summary="PRIVATE SUMMARY")
        transcript.prompt(4, text="PRIVATE FOLLOW UP")
        transcript.request(5, read=20_000, write_1h=5_000, tools=(("t2", "Read", "/secret/place/a.py"),))
        (record,) = self.measure(transcript)
        stored = json.dumps(record)
        for private in ("PRIVATE", "/secret/place", "a.py"):
            self.assertNotIn(private, stored)

    def test_a_compaction_with_nothing_before_it_is_left_out(self) -> None:
        transcript = Transcript()
        transcript.compact(0, pre=0)
        transcript.prompt(1)
        transcript.request(2, write_1h=20_000)
        self.assertEqual(self.measure(transcript), [])

    def test_a_transcript_that_only_talks_about_the_marker_holds_no_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            talk = Transcript()
            talk.prompt(0, text='grep for "subtype":"compact_boundary" please')
            talk_path = talk.write(Path(tmp, "talk.jsonl"))
            real_path = one_compaction().write(Path(tmp, "real.jsonl"))
            self.assertFalse(compaction_outcomes.has_compaction(talk_path))
            self.assertEqual(compaction_outcomes.compaction_markers(real_path), 1)


def record(**overrides) -> dict:
    """A measured record with round numbers, priced below at $10 / $50 per
    million so every figure can be worked by hand."""
    base = {
        "id": "s1:b1", "session_id": "s1", "model": "test-model", "trigger": "manual",
        "boundary_at": T0.isoformat(), "pre_tokens": 300_000, "summary_chars": 4_000,
        "before": {"context": 300_000},
        "first_after": {"context": 100_000, "cache_write_5m": 0, "cache_write_1h": 90_000},
        "gap_seconds": 60, "cache_ttl_seconds": 3600,
        "after": {
            "prompts": 1, "requests": 5, "rebuilds_5m": 0, "rebuilds_1h": 0, "rereads": 1, "reread_chars": 8_000,
            "tokens": {"input": 50, "output": 1_000, "cache_write_5m": 0, "cache_write_1h": 110_000, "cache_read": 400_000},
        },
        "closed_by": None,
    }
    base.update(overrides)
    return base


PRICE = {"in": 10.0, "out": 50.0}


class FiguresTests(unittest.TestCase):
    def test_a_compaction_followed_by_a_few_requests_costs_more_than_it_saves(self) -> None:
        with patch.object(compaction_outcomes, "lookup", return_value=PRICE):
            fig = compaction_outcomes.figures(record())
        # actual: 50 + 2 x 110,000 + 0.1 x 400,000 input units at $10/M, plus 1,000 output at $50/M
        self.assertAlmostEqual(fig["actual_usd"], 2.6505)
        # 200k removed, re-sent from cache by each of the 5 requests: 5 x 0.1 x 200,000 at $10/M
        # less the rebuild compacting caused: 90,000 x (2.0 - 0.1) at $10/M
        self.assertAlmostEqual(fig["rebuild_usd"], 1.71)
        self.assertAlmostEqual(fig["without_usd"], 2.6505 + 1.0 - 1.71)
        # summary call: 300k read from cache (or not), plus 1,000 tokens of summary written
        self.assertAlmostEqual(fig["summarise_usd_low"], 0.35)
        self.assertAlmostEqual(fig["summarise_usd_high"], 3.05)
        self.assertLess(fig["saved_usd_high"], 0)
        # (1.71 + 0.35) / (200,000 x 0.1 at $10/M)
        self.assertAlmostEqual(fig["break_even_requests"], 10.3)
        self.assertEqual(fig["reread_tokens_est"], 2_000)
        self.assertAlmostEqual(fig["reread_share"], 0.01)

    def test_a_pause_past_the_cache_lifetime_is_not_charged_to_compacting(self) -> None:
        with patch.object(compaction_outcomes, "lookup", return_value=PRICE):
            fig = compaction_outcomes.figures(record(gap_seconds=4_000))
        self.assertEqual(fig["rebuild_usd"], 0.0)
        # Without compacting the first request rebuilds the removed 200k too, at the 1h write rate,
        # and the other four re-send it: 200,000 x (4 x 0.1 + 2.0) at $10/M.
        self.assertAlmostEqual(fig["without_usd"], 2.6505 + 4.8)

    def test_rebuilds_later_in_the_stretch_carry_the_removed_history_at_the_write_rate(self) -> None:
        after = {**record()["after"], "rebuilds_1h": 2}
        with patch.object(compaction_outcomes, "lookup", return_value=PRICE):
            fig = compaction_outcomes.figures(record(after=after))
        # 3 requests re-send 200k at 0.1, 2 rebuild it at 2.0
        self.assertAlmostEqual(fig["without_usd"], 2.6505 + 200_000 * 1e-5 * (3 * 0.1 + 2 * 2.0) - 1.71)

    def test_an_auto_compaction_has_nothing_to_weigh_against(self) -> None:
        with patch.object(compaction_outcomes, "lookup", return_value=PRICE):
            fig = compaction_outcomes.figures(record(trigger="auto"))
        self.assertFalse(fig["measurable"])
        self.assertIn("on its own", fig["reason"])
        self.assertEqual(fig["carried"], 200_000)
        self.assertNotIn("saved_usd_low", fig)

    def test_no_request_after_yet_is_not_measurable(self) -> None:
        fig = compaction_outcomes.figures(record(first_after=None))
        self.assertFalse(fig["measurable"])
        self.assertEqual(fig["reason"], "No request after the compaction yet.")

    def test_an_unpriced_model_says_so_instead_of_pricing_at_zero(self) -> None:
        with patch.object(compaction_outcomes, "lookup", return_value=None):
            fig = compaction_outcomes.figures(record())
        self.assertFalse(fig["measurable"])
        self.assertIn("No list price for test-model", fig["reason"])


class AttachNudgeTests(unittest.TestCase):
    def test_each_compaction_gets_the_nudge_shown_since_the_one_before(self) -> None:
        records = [
            {"session_id": "s1", "boundary_at": (T0 + timedelta(minutes=10)).isoformat()},
            {"session_id": "s1", "boundary_at": (T0 + timedelta(minutes=60)).isoformat()},
            {"session_id": "s1", "boundary_at": (T0 + timedelta(minutes=90)).isoformat()},
        ]
        receipts = [
            {"id": "early", "session_id": "s1", "created_at": (T0 + timedelta(minutes=5)).isoformat(), "decision": "later"},
            {"id": "copied", "session_id": "s1", "created_at": (T0 + timedelta(minutes=30)).isoformat(), "decision": "copied"},
            {"id": "other", "session_id": "s2", "created_at": (T0 + timedelta(minutes=70)).isoformat(), "decision": "copied"},
            {"id": "after", "session_id": "s1", "created_at": (T0 + timedelta(minutes=95)).isoformat(), "decision": None},
        ]
        compaction_outcomes.attach_nudges(records, receipts)
        self.assertEqual(records[0]["nudge"]["id"], "early")
        self.assertEqual(records[0]["nudge"]["decision"], "later")
        self.assertEqual(records[1]["nudge"]["id"], "copied")
        self.assertIsNone(records[2]["nudge"])

    def test_a_receipt_opened_after_the_command_was_typed_did_not_lead_to_it(self) -> None:
        # 2026-09-09: /compact typed at 12:14:31.24, receipt opened at 12:14:31.75,
        # boundary written a minute later.
        records = [{
            "session_id": "s1",
            "command_at": (T0 + timedelta(seconds=31.24)).isoformat(),
            "boundary_at": (T0 + timedelta(seconds=91)).isoformat(),
        }]
        receipts = [{"id": "late", "session_id": "s1", "created_at": (T0 + timedelta(seconds=31.75)).isoformat(), "decision": None}]
        compaction_outcomes.attach_nudges(records, receipts)
        self.assertIsNone(records[0]["nudge"])


class StoredOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": str(Path(self.tmp.name, "state.json"))})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_a_poll_over_an_unchanged_transcript_writes_nothing(self) -> None:
        self.assertEqual(local_state.upsert_compaction_outcomes([{**record(), "measured_at": "one"}]), 1)
        self.assertEqual(local_state.upsert_compaction_outcomes([{**record(), "measured_at": "two"}]), 0)
        grown = record(after={**record()["after"], "prompts": 4})
        self.assertEqual(local_state.upsert_compaction_outcomes([grown]), 1)
        (stored,) = local_state.compaction_outcomes()
        self.assertEqual(stored["after"]["prompts"], 4)

    def test_record_session_keeps_the_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = one_compaction().write(Path(tmp, "s1.jsonl"))
            compaction_outcomes.record_session("s1", path)
        (stored,) = local_state.compaction_outcomes()
        self.assertEqual(stored["id"], "s1:row-5")
        self.assertIn("measured_at", stored)

    def test_the_cli_fills_in_from_transcripts_and_reports(self) -> None:
        projects = Path(self.tmp.name, "projects")
        (projects / "-repo").mkdir(parents=True)
        one_compaction().write(projects / "-repo" / "s1.jsonl")
        output = io.StringIO()
        with patch("aiwatcher_cli.scanner.CLAUDE_PROJECTS_DIRS", [projects]), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["compactions", "--days", "1"]), 0)
        text = output.getvalue()
        self.assertIn("Compactions recorded: 1", text)
        self.assertIn("300.0k -> 60.0k", text)
        self.assertIn("No compaction has a full 10-prompt stretch", text)


class ReportTests(unittest.TestCase):
    def test_nothing_recorded_says_when_records_start(self) -> None:
        self.assertIn("No compactions recorded yet", compaction_outcomes.render_report([]))

    def test_an_auto_compaction_stays_out_of_the_total(self) -> None:
        text = compaction_outcomes.render_report([record(trigger="auto", closed_by="prompts")], now=T0)
        self.assertIn("nothing to weigh a saving against", text)
        self.assertIn("No compaction has a full", text)


class LiveRecorderTests(unittest.TestCase):
    """The Companion's poll keeps records current without parsing a transcript
    on every tick."""

    def setUp(self) -> None:
        ui._OUTCOME_SCAN.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "s1.jsonl")
        self.session = LocalSession(
            session_id="s1", tool="claude-code", project_path=None, source_path=str(self.path),
            model="claude-opus-5", started_at=T0, updated_at=T0,
        )
        self.row = SimpleNamespace(session_id="s1", live=True, analyst_run=False)

    def tearDown(self) -> None:
        ui._OUTCOME_SCAN.clear()
        self.tmp.cleanup()

    def poll(self, recorder, row=None) -> None:
        ui._record_compaction_outcomes([row or self.row], [self.session])

    def test_it_parses_only_when_a_compaction_is_new_or_still_open(self) -> None:
        transcript = Transcript()
        transcript.prompt(0)
        transcript.request(1, write_1h=40_000)
        transcript.write(self.path)
        with patch.object(compaction_outcomes, "record_session", return_value=[{"closed_by": "prompts"}]) as recorder:
            self.poll(recorder)
            recorder.assert_not_called()                     # no compaction in the file
            transcript.compact(2, pre=40_010)
            transcript.write(self.path)
            self.poll(recorder)
            self.assertEqual(recorder.call_count, 1)         # a new one
            self.poll(recorder)
            self.assertEqual(recorder.call_count, 1)         # file unchanged
            transcript.prompt(3)
            transcript.write(self.path)
            self.poll(recorder)
            self.assertEqual(recorder.call_count, 1)         # grew, but its stretch is closed
            recorder.return_value = [{"closed_by": None}]
            transcript.compact(4, pre=40_010)
            transcript.write(self.path)
            self.poll(recorder)
            transcript.prompt(5)
            transcript.write(self.path)
            self.poll(recorder)
            self.assertEqual(recorder.call_count, 3)         # a new one, then again while it is open

    def test_a_session_that_is_not_live_is_left_alone(self) -> None:
        one_compaction().write(self.path)
        with patch.object(compaction_outcomes, "record_session", return_value=[]) as recorder:
            self.poll(recorder, SimpleNamespace(session_id="s1", live=False, analyst_run=False))
        recorder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
