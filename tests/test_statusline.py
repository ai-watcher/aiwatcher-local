from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.statusline import (
    CRITICAL_TOKENS,
    UNCOMMITTED_ALARM_USD,
    UNCOMMITTED_NOTICE_USD,
    build_statusline,
    read_transcript,
    statusline_from_stdin,
    statusline_settings_snippet,
)


def turn(
    *,
    when: datetime,
    cache_read: int = 0,
    cache_write: int = 0,
    plain_in: int = 10,
    out: int = 200,
    model: str = "claude-sonnet-5",
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

        # 1M cache reads at $3.00/Mtok x 0.1 = $0.30, plus a negligible 10
        # uncached tokens. Reading input_tokens alone would give ~$0.00003.
        self.assertGreater(stats["total_usd"], 0.25)
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

    def test_flags_context_that_needs_compacting(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as repo:
            init_repo(repo, commit_at=now - timedelta(hours=2))
            path = Path(repo, "t.jsonl")
            write_transcript(path, [turn(when=now, cache_read=CRITICAL_TOKENS + 50_000)])
            line = build_statusline(self._payload(path, repo))

        self.assertIn("compact", line)

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
