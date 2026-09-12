from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.pricing import estimate_cost
from aiwatcher_cli.statusline import (
    UNCOMMITTED_ALARM_USD,
    UNCOMMITTED_NOTICE_USD,
    build_statusline,
    read_transcript,
    statusline_from_stdin,
    statusline_settings_snippet,
)

MODEL = "claude-sonnet-5"


def turn(
    *,
    when: datetime,
    cache_read: int = 0,
    cache_write: int = 0,
    plain_in: int = 10,
    out: int = 200,
    model: str = MODEL,
) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {
            "model": model,
            "usage": {
                "input_tokens": plain_in,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


def write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def init_repo(path: str, *, commit_at: datetime) -> None:
    def run(args: list[str], env: dict[str, str] | None = None) -> None:
        result = subprocess.run(args, cwd=path, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
    run(["git", "init", "-q", "."])
    run(["git", "config", "user.email", "dev@example.com"])
    run(["git", "config", "user.name", "Dev"])
    Path(path, "a.txt").write_text("x\n", encoding="utf-8")
    run(["git", "add", "a.txt"])
    stamp = commit_at.strftime("%Y-%m-%dT%H:%M:%S%z")
    run(["git", "commit", "-m", "base"],
        {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


class TranscriptReadingTests(unittest.TestCase):
    """The statusline must agree with the rest of the product about cost.

    Cached input is most of the bill on a long session, so reading
    `input_tokens` alone would understate it by roughly 11x -- the same defect
    the scanner carried before #48. This reuses the scanner's own splitter.
    """

    def test_cached_tokens_are_counted_and_priced(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=1_000_000, plain_in=10, out=0)])
            stats = read_transcript(str(path))

        # The defect this guards is an order-of-magnitude one: reading
        # input_tokens alone prices 10 tokens instead of 1,000,010, so the bill
        # comes out around $0.00003. Comparing against the rate table rather
        # than a fixed figure keeps that claim true at any rate.
        input_only = estimate_cost(MODEL, 10, 0, when=now)
        self.assertGreater(stats["total_usd"], input_only * 1_000)
        self.assertEqual(stats["latest_context"], 1_000_010)

    def test_context_reports_the_latest_turn_and_the_peak(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work, "t.jsonl")
            write_transcript(path, [
                turn(when=now - timedelta(minutes=3), cache_read=500_000),
                turn(when=now - timedelta(minutes=2), cache_read=900_000),
                turn(when=now - timedelta(minutes=1), cache_read=700_000),
            ])
            stats = read_transcript(str(path))

        self.assertEqual(stats["turns"], 3)
        self.assertEqual(stats["latest_context"], 700_010)
        self.assertEqual(stats["peak_context"], 900_010)

    def test_since_splits_out_spend_after_a_timestamp(self) -> None:
        now = datetime.now(timezone.utc)
        cut = now - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work, "t.jsonl")
            write_transcript(path, [
                turn(when=now - timedelta(hours=3), cache_read=1_000_000),
                turn(when=now - timedelta(minutes=30), cache_read=1_000_000),
            ])
            stats = read_transcript(str(path), since=cut)

        self.assertAlmostEqual(stats["since_usd"], stats["total_usd"] / 2, places=4)

    def test_a_missing_transcript_is_not_an_error(self) -> None:
        stats = read_transcript("/no/such/transcript.jsonl")
        self.assertFalse(stats["available"])
        self.assertEqual(stats["total_usd"], 0.0)

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work, "t.jsonl")
            write_transcript(path, [
                "{not json",
                "",
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                turn(when=now, cache_read=100_000),
            ])
            stats = read_transcript(str(path))

        self.assertTrue(stats["available"])
        self.assertEqual(stats["turns"], 1)


class BoundaryViewTests(unittest.TestCase):
    """What `since` adds for compaction: the context just before it, the
    smallest after it, the calls and prompts after it, the files touched."""

    def test_the_boundary_fields_split_the_transcript_at_since(self) -> None:
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=10)

        def call(when: datetime, context: int, files: list[str] = ()) -> str:
            return json.dumps({
                "type": "assistant",
                "timestamp": when.isoformat().replace("+00:00", "Z"),
                "message": {
                    "model": MODEL,
                    "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": context - 10},
                    "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": f}} for f in files],
                },
            })

        def typed(when: datetime) -> str:
            return json.dumps({"type": "user", "timestamp": when.isoformat().replace("+00:00", "Z"),
                               "message": {"role": "user", "content": "do the thing"}})

        def tool_result(when: datetime) -> str:
            return json.dumps({"type": "user", "timestamp": when.isoformat().replace("+00:00", "Z"),
                               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}})

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.jsonl")
            write_transcript(path, [
                typed(since - timedelta(minutes=30)),
                call(since - timedelta(minutes=29), 60_000),
                call(since - timedelta(minutes=5), 300_000),
                typed(since + timedelta(minutes=1)),
                call(since + timedelta(minutes=2), 310_000, ["/repo/a.py"]),
                tool_result(since + timedelta(minutes=2, seconds=30)),
                call(since + timedelta(minutes=3), 320_000, ["/repo/a.py", "/repo/b.py"]),
            ])
            stats = read_transcript(str(path), since=since)

        self.assertEqual(stats["first_context"], 60_000)
        self.assertEqual(stats["context_at_since"], 300_000)
        self.assertEqual(stats["min_context_since"], 310_000)
        self.assertEqual(stats["turns_since"], 2)
        # One typed prompt after `since`; the tool result is not a prompt.
        self.assertEqual(stats["prompts_since"], 1)
        self.assertEqual(stats["files_since"], ["/repo/a.py", "/repo/b.py"])
        # Nothing compacted: no title, no markers, and the smallest call after
        # `since` was the first one, with one call after it.
        self.assertIsNone(stats["title"])
        self.assertIsNone(stats["command_seen_at"])
        self.assertIsNone(stats["boundary_seen_at"])
        self.assertEqual(stats["turns_after_min_since"], 1)

    def test_the_compaction_trail_is_read_as_the_tool_writes_it(self) -> None:
        """Three rows, in the order Claude Code writes them: the /compact the
        user typed, the boundary plus summary a minute later, then the first
        reply at the new size. Each is a fact the bar can move on, well
        before the reply that used to be the only signal."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=10)

        def call(when: datetime, context: int) -> str:
            return json.dumps({
                "type": "assistant", "timestamp": when.isoformat().replace("+00:00", "Z"),
                "message": {"model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": context - 10}, "content": []},
            })

        def row(kind: str, when: datetime, **extra: object) -> str:
            return json.dumps({"type": kind, "timestamp": when.isoformat().replace("+00:00", "Z"), **extra})

        typed_at = since + timedelta(minutes=3)
        boundary_at = since + timedelta(minutes=4)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.jsonl")
            write_transcript(path, [
                json.dumps({"type": "custom-title", "customTitle": "Context health calibration"}),
                call(since - timedelta(minutes=5), 500_000),
                row("user", since + timedelta(minutes=1), message={"role": "user", "content": "keep going"}),
                call(since + timedelta(minutes=2), 593_000),
                row("user", typed_at, message={"role": "user", "content": "<command-name>/compact</command-name> <command-args>Keep everything since…</command-args>"}),
                row("system", boundary_at, subtype="compact_boundary"),
                row("user", boundary_at, isCompactSummary=True, message={"role": "user", "content": "This session is being continued…"}),
                call(since + timedelta(minutes=6), 80_000),
            ])
            stats = read_transcript(str(path), since=since)

        self.assertEqual(stats["title"], "Context health calibration")
        self.assertEqual(stats["command_seen_at"], typed_at)
        self.assertEqual(stats["boundary_seen_at"], boundary_at)
        # The drop: the reply after the boundary is the smallest since the
        # commit, nothing has come after it yet, and the one before it is the
        # size it shed from.
        self.assertEqual(stats["min_context_since"], 80_000)
        self.assertEqual(stats["turns_after_min_since"], 0)
        self.assertEqual(stats["prompts_after_min_since"], 0)
        self.assertEqual(stats["context_before_min_since"], 593_000)
        # The slash command and the summary are rows, not turns a person
        # would count: one prompt since the commit, not three.
        self.assertEqual(stats["prompts_since"], 1)

    def test_rows_the_tool_injects_are_not_prompts_and_the_typed_one_ends_the_shed(self) -> None:
        """After the small reply, Claude Code writes the command's stdout, a
        background-task notification and tool results as user rows, none of
        them typed. The count of prompts after the shed stays at zero until
        the person types -- that is the line the confirmed step ends on."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=10)

        def call(when: datetime, context: int) -> str:
            return json.dumps({
                "type": "assistant", "timestamp": when.isoformat().replace("+00:00", "Z"),
                "message": {"model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": context - 10}, "content": []},
            })

        def user(when: datetime, content: object, **extra: object) -> str:
            return json.dumps({"type": "user", "timestamp": when.isoformat().replace("+00:00", "Z"),
                               "message": {"role": "user", "content": content}, **extra})

        shed_at = since + timedelta(minutes=6)
        injected = [
            call(since - timedelta(minutes=5), 500_000),
            user(since + timedelta(minutes=1), "keep going"),
            call(since + timedelta(minutes=2), 593_000),
            call(shed_at, 80_000),
            user(shed_at + timedelta(seconds=1), "<local-command-stdout>Compacted </local-command-stdout>"),
            user(shed_at + timedelta(seconds=2), "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
            user(shed_at + timedelta(seconds=3), "<system-reminder>\nsomething\n</system-reminder>"),
            user(shed_at + timedelta(seconds=4), [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
            call(shed_at + timedelta(seconds=10), 84_000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.jsonl")
            write_transcript(path, injected)
            before_typing = read_transcript(str(path), since=since)
            # The typed message carries a screenshot, as the real one did.
            write_transcript(path, injected + [user(shed_at + timedelta(minutes=3), [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
                {"type": "text", "text": "now do the next thing"},
            ])])
            after_typing = read_transcript(str(path), since=since)

        self.assertEqual(before_typing["min_context_since"], 80_000)
        self.assertEqual(before_typing["turns_after_min_since"], 1)
        self.assertEqual(before_typing["prompts_after_min_since"], 0)
        self.assertEqual(before_typing["prompts_since"], 1)
        self.assertEqual(after_typing["prompts_after_min_since"], 1)
        self.assertEqual(after_typing["prompts_since"], 2)

    def test_rows_the_tool_writes_again_at_compaction_are_read_once(self) -> None:
        """Shape of the real log on 2026-09-09: Claude Code appended 1,333
        copies of morning rows (same uuid, same timestamp) after the live
        tail and just before the boundary. Read in file order, the "latest"
        context was a 591k row from 12:03 instead of the live 320k, the
        receipt closed on that number and the bar skipped Compacted."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=10)

        def call(when: datetime, context: int, uuid: str) -> str:
            return json.dumps({
                "type": "assistant", "uuid": uuid, "timestamp": when.isoformat().replace("+00:00", "Z"),
                "message": {"model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": context - 10}, "content": []},
            })

        def row(kind: str, when: datetime, uuid: str, **extra: object) -> str:
            return json.dumps({"type": kind, "uuid": uuid, "timestamp": when.isoformat().replace("+00:00", "Z"), **extra})

        morning = call(since - timedelta(hours=2), 591_757, "morning")
        typed = row("user", since - timedelta(hours=2, minutes=1), "typed", message={"role": "user", "content": "go"})
        live_tail = call(since + timedelta(minutes=2), 319_921, "tail")
        boundary_at = since + timedelta(minutes=4)
        at_the_boundary = [
            typed, morning,
            call(since - timedelta(minutes=5), 300_000, "at-commit"),
            live_tail,
            # The copies, then the boundary, before any reply at the new size.
            typed, morning,
            row("system", boundary_at, "boundary", subtype="compact_boundary"),
            row("user", boundary_at, "summary", isCompactSummary=True, message={"role": "user", "content": "This session is being continued…"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.jsonl")
            write_transcript(path, at_the_boundary)
            at_boundary = read_transcript(str(path), since=since)
            write_transcript(path, at_the_boundary + [call(since + timedelta(minutes=6), 75_937, "reply")])
            after_reply = read_transcript(str(path), since=since)

        # The copy does not become the current size, and is not a second turn.
        self.assertEqual(at_boundary["latest_context"], 319_921)
        self.assertEqual(at_boundary["turns"], 3)
        self.assertEqual(at_boundary["turns_since"], 1)
        self.assertEqual(at_boundary["boundary_seen_at"], boundary_at)
        # The reply shed from the live tail, not from the copy.
        self.assertEqual(after_reply["min_context_since"], 75_937)
        self.assertEqual(after_reply["context_before_min_since"], 319_921)
        self.assertEqual(after_reply["turns_after_min_since"], 0)


class StatuslineRenderingTests(unittest.TestCase):
    def _payload(self, transcript: Path, repo: str) -> dict[str, object]:
        return {
            "transcript_path": str(transcript),
            "workspace": {"current_dir": repo},
            "cwd": repo,
        }

    def test_leads_with_spend_since_the_last_commit(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            # Enough cached reads to clear the notice threshold comfortably.
            write_transcript(path, [
                turn(when=now - timedelta(minutes=10), cache_read=40_000_000),
            ])
            line = build_statusline(self._payload(path, repo))

        self.assertIn("since commit", line)
        self.assertTrue(line.startswith("*") or line.startswith("!"))
        self.assertIn("session", line)

    def test_stays_quiet_about_small_spend_since_the_last_commit(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=100_000)])
            line = build_statusline(self._payload(path, repo))

        self.assertNotIn("since commit", line)
        self.assertIn("session", line)

    def test_escalates_the_marker_past_the_alarm_threshold(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=200_000_000)])
            line = build_statusline(self._payload(path, repo))

        self.assertTrue(line.startswith("!"), line)

    def test_flags_context_at_the_models_own_window(self) -> None:
        # The limit is the model's, not a constant: 200K fills Haiku's window
        # and is a fifth of Sonnet 5's.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=199_990, model="claude-haiku-4-5")])
            haiku = build_statusline(self._payload(path, repo))
            write_transcript(path, [turn(when=now, cache_read=199_990, model="claude-sonnet-5")])
            sonnet = build_statusline(self._payload(path, repo))

        self.assertIn("compact", haiku)
        self.assertNotIn("compact", sonnet)

    def test_a_turn_bigger_than_the_table_allows_means_the_table_is_stale(self) -> None:
        # A 250K turn on a "200K" model was accepted by the provider, so the
        # window is not 200K. Reporting it as over the limit would be the old
        # bug with a lookup in front of it.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=250_000, model="claude-haiku-4-5")])
            line = build_statusline(self._payload(path, repo))

        self.assertIn("/turn", line)
        self.assertNotIn("compact", line)

    def test_an_unknown_model_gets_the_number_and_no_verdict(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=900_000, model="model-nobody-knows")])
            line = build_statusline(self._payload(path, repo))

        self.assertIn("/turn", line)
        self.assertNotIn("compact", line)

    def test_output_is_ascii_so_it_cannot_break_a_cp1252_console(self) -> None:
        # A middle-dot separator raises UnicodeEncodeError under cp1252 and
        # takes the whole status line with it.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=40_000_000)])
            line = build_statusline(self._payload(path, repo))

        line.encode("cp1252")  # must not raise
        self.assertTrue(line.isascii())

    def test_empty_transcript_renders_nothing(self) -> None:
        # Better than a row of zeroes: this sits under every prompt, and
        # anything always present stops being read.
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=datetime.now(timezone.utc))
            path = Path(repo, "t.jsonl")
            write_transcript(path, ["{}"])
            self.assertEqual(build_statusline(self._payload(path, repo)), "")

    def test_works_outside_a_git_repo(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as plain:
            path = Path(plain, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=40_000_000)])
            line = build_statusline({
                "transcript_path": str(path),
                "workspace": {"current_dir": plain},
            })

        self.assertIn("session", line)
        self.assertNotIn("since commit", line)


class FailureModeTests(unittest.TestCase):
    """A status line that throws replaces itself with an error on every
    prompt. Silence is the only acceptable failure mode."""

    def test_garbage_stdin_renders_nothing(self) -> None:
        self.assertEqual(statusline_from_stdin("not json at all"), "")
        self.assertEqual(statusline_from_stdin(""), "")
        self.assertEqual(statusline_from_stdin("[]"), "")
        self.assertEqual(statusline_from_stdin("null"), "")

    def test_payload_without_a_transcript_renders_nothing(self) -> None:
        self.assertEqual(statusline_from_stdin(json.dumps({"cwd": "/tmp"})), "")
        self.assertEqual(statusline_from_stdin(json.dumps({"transcript_path": None})), "")

    def test_settings_snippet_targets_the_status_line_key(self) -> None:
        snippet = statusline_settings_snippet("aiwatcher")
        self.assertEqual(snippet["statusLine"]["type"], "command")
        self.assertEqual(snippet["statusLine"]["command"], "aiwatcher statusline")


class ThresholdTests(unittest.TestCase):
    def test_alarm_is_above_notice(self) -> None:
        self.assertGreater(UNCOMMITTED_ALARM_USD, UNCOMMITTED_NOTICE_USD)


if __name__ == "__main__":
    unittest.main()
