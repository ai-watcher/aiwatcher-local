"""Incremental reads must preserve accounting and stay bounded on live logs."""
import json
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import compaction, compaction_outcomes, statusline, transcript_cache, ui
from aiwatcher_cli.scanner import LocalSession


class TranscriptCacheTests(unittest.TestCase):
    def setUp(self):
        transcript_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "session.jsonl")
        self.consumed = 0

    def tearDown(self):
        transcript_cache.clear()
        self.tmp.cleanup()

    def append(self, *rows):
        with self.path.open("ab") as handle:
            for row in rows:
                handle.write(json.dumps(row).encode() + b"\n")

    def total(self, namespace="sum"):
        def consume(state, row):
            self.consumed += 1
            state['total'] += row.get('n', 0)
        with transcript_cache.folded_jsonl(str(self.path), namespace, lambda: {'total': 0}, consume) as state:
            return state['total'] if state is not None else None

    def test_unchanged_and_append_only_consume_new_rows(self):
        self.append(*({'n': 1} for _ in range(500)))
        self.assertEqual(self.total(), 500)
        with patch.object(transcript_cache, 'open', side_effect=AssertionError('unchanged file opened')):
            self.assertEqual(self.total(), 500)
        self.append({'n': 3})
        self.assertEqual(self.total(), 503)
        self.assertEqual(self.consumed, 501)
        # A new accounting window has independent state.
        self.assertEqual(self.total('new-window'), 503)
        self.assertEqual(self.consumed, 1002)

    def test_append_reads_bytes_proportional_to_the_append(self):
        self.append(*({'n': 1, 'output': 'x' * 1000} for _ in range(1000)))
        self.total()
        self.append({'n': 2})
        bytes_read = 0
        real_open = open
        class MeteredFile:
            def __init__(self, *args, **kwargs):
                self.handle = real_open(*args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def read(self, *args):
                nonlocal bytes_read
                data = self.handle.read(*args)
                bytes_read += len(data)
                return data
            def readline(self, *args):
                nonlocal bytes_read
                data = self.handle.readline(*args)
                bytes_read += len(data)
                return data
        with patch.object(transcript_cache, 'open', MeteredFile):
            self.assertEqual(self.total(), 1002)
        # Four small guards plus one new row, rather than the 1 MB history.
        self.assertLess(bytes_read, 2048)

    def test_partial_record_is_retried_without_double_counting(self):
        self.append({'n': 1})
        with self.path.open('ab') as handle:
            handle.write(b'{"n":')
        self.assertEqual(self.total(), 1)
        with self.path.open('ab') as handle:
            handle.write(b'2}\n')
        self.assertEqual(self.total(), 3)
        self.assertEqual(self.consumed, 2)

    def test_valid_unterminated_record_is_rebuilt_if_extended(self):
        self.path.write_bytes(b'{"n":1}')
        self.assertEqual(self.total(), 1)
        with self.path.open('ab') as handle:
            handle.write(b'\n{"n":2}\n')
        self.assertEqual(self.total(), 3)

    def test_replacement_shrink_same_size_edit_and_changed_append_guard(self):
        self.append({'n': 1}, {'n': 2})
        self.assertEqual(self.total(), 3)
        replacement = self.path.with_suffix('.new')
        replacement.write_bytes(b'{"n":4}\n')
        os.replace(replacement, self.path)
        self.assertEqual(self.total(), 4)
        self.path.write_bytes(b'{"n":5}\n')
        self.assertEqual(self.total(), 5)
        self.path.write_bytes(b'{"n":6}\n{"n":7}\n')
        self.assertEqual(self.total(), 13)
        self.path.write_bytes(b'{"n":8}\n')
        self.assertEqual(self.total(), 8)

    def test_missing_oversized_and_consumer_failure_do_not_return_partial_state(self):
        self.assertIsNone(self.total())
        self.append({'n': 1}, {'n': 22})
        with patch.object(transcript_cache, 'MAX_LINE_BYTES', 9):
            self.assertIsNone(self.total())
        self.assertEqual(self.total(), 23)
        def broken(state, row):
            state['total'] += 1
            raise ValueError('bad receipt')
        with self.assertRaises(ValueError):
            with transcript_cache.folded_jsonl(str(self.path), 'broken', lambda: {'total': 0}, broken):
                pass
        self.assertFalse(any(key[-1] == 'broken' for key in transcript_cache._CACHE))

    def test_eof_during_a_concurrent_truncate_cannot_spin_or_report_partial_totals(self):
        self.append({'n': 1})
        real_open = open
        class TruncatedFile:
            def __init__(self, *args, **kwargs):
                self.handle = real_open(*args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def read(self, *args):
                return b''
            def readline(self, *args):
                return b''
        with patch.object(transcript_cache, 'open', TruncatedFile):
            self.assertIsNone(self.total())
            with self.assertRaises(OSError):
                transcript_cache.count_pattern(str(self.path), re.compile(b'marker'))
        self.assertEqual(len(transcript_cache._CACHE), 0)

    def test_bounds_apply_to_entries_and_retained_record_counts(self):
        self.append({'n': 1}, {'n': 2})
        with patch.object(transcript_cache, 'MAX_ENTRIES', 3):
            for i in range(8):
                self.assertEqual(self.total(i), 3)
            self.assertEqual(len(transcript_cache._CACHE), 3)
        transcript_cache.clear()
        with patch.object(transcript_cache, 'MAX_TOTAL_RECORDS', 3):
            self.total('one')
            self.total('two')
            self.assertEqual(len(transcript_cache._CACHE), 1)
        with patch.object(transcript_cache, 'MAX_RECORDS_PER_ENTRY', 1):
            self.assertEqual(self.total('oversized'), 3)
            self.assertFalse(any(key[-1] == 'oversized' for key in transcript_cache._CACHE))

    def test_concurrent_readers_do_not_double_count_append(self):
        self.append({'n': 1})
        self.total()
        self.append({'n': 2})
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: self.total(), range(16))), [3] * 16)
        self.assertEqual(self.consumed, 2)

    def test_marker_count_handles_chunk_and_append_seams_and_replacement(self):
        pattern = re.compile(rb'"subtype":\s?"compact_boundary"')
        marker = b'"subtype": "compact_boundary"'
        self.path.write_bytes(b'padding' + marker + marker[:12])
        with patch.object(transcript_cache, 'CHUNK_BYTES', 7):
            self.assertEqual(transcript_cache.count_pattern(str(self.path), pattern), 1)
            with self.path.open('ab') as handle:
                handle.write(marker[12:])
            self.assertEqual(transcript_cache.count_pattern(str(self.path), pattern), 2)
        self.path.write_bytes(b'no markers')
        self.assertEqual(transcript_cache.count_pattern(str(self.path), pattern), 0)

    def test_claude_incremental_accounting_matches_fresh_fold_and_snapshots(self):
        stamp = '2026-10-17T12:00:00+00:00'
        def request(uuid, request_id, n):
            return {'type': 'assistant', 'uuid': uuid, 'requestId': request_id, 'timestamp': stamp,
                    'message': {'model': 'claude-sonnet-4-5', 'usage': {'input_tokens': n, 'output_tokens': 10},
                                'content': [{'type': 'tool_use', 'name': 'Read', 'id': uuid,
                                             'input': {'file_path': f'{uuid}.py'}}]}}
        since = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.append(request('one', 'r1', 100))
        before = statusline.read_transcript(str(self.path), since=since)
        compaction_outcomes._read_timeline(str(self.path))
        self.append(request('one', 'r1', 100), request('copy', 'r1', 100),
                    {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'boundary', 'timestamp': stamp},
                    request('two', 'r2', 30))
        warm = statusline.read_transcript(str(self.path), since=since)
        warm_timeline = compaction_outcomes._read_timeline(str(self.path))
        self.assertEqual(warm['turns'], 2)
        self.assertEqual(before['files_since'], ['one.py'])
        transcript_cache.clear()
        self.assertEqual(warm, statusline.read_transcript(str(self.path), since=since))
        self.assertEqual(warm_timeline, compaction_outcomes._read_timeline(str(self.path)))

    def test_codex_incremental_accounting_matches_fresh_fold(self):
        def receipt(total, context):
            return {'type': 'event_msg', 'timestamp': '2026-10-17T12:00:00Z',
                    'payload': {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': total},
                                'last_token_usage': {'input_tokens': context}}}}
        since = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.append(receipt(100, 90))
        compaction.codex_boundary_stats(str(self.path), since=since)
        self.append(receipt(100, 90), receipt(140, 30))
        warm = compaction.codex_boundary_stats(str(self.path), since=since)
        self.assertEqual(warm['turns'], 2)
        transcript_cache.clear()
        self.assertEqual(warm, compaction.codex_boundary_stats(str(self.path), since=since))

    def test_outcome_no_marker_poll_path_remains_bounded(self):
        ui._OUTCOME_SCAN.clear()
        self.path.write_bytes(b'{}\n')
        try:
            with patch.object(compaction_outcomes, 'compaction_markers', return_value=0):
                for i in range(100):
                    session = LocalSession(session_id=str(i), tool='claude-code',
                                           source_path=str(self.path), project_path=None)
                    row = SimpleNamespace(session_id=str(i), live=True, analyst_run=False)
                    # Different names simulate successive live sessions without
                    # creating or scanning a user's actual logs.
                    session.source_path = str(self.path.parent / f'{i}.jsonl')
                    with patch.object(ui.os, 'stat', return_value=self.path.stat()):
                        ui._record_compaction_outcomes([row], [session])
                    self.assertLessEqual(len(ui._OUTCOME_SCAN), 64)
            self.assertEqual(len(ui._OUTCOME_SCAN), 64)
        finally:
            ui._OUTCOME_SCAN.clear()

    def test_outcome_fast_paths_are_bounded_and_same_size_rewrites_are_measured(self):
        ui._OUTCOME_SCAN.clear()
        try:
            for i in range(100):
                ui._remember_outcome_scan(str(i), (1, i, 0, 1, 1), 0, True)
            self.assertEqual(len(ui._OUTCOME_SCAN), 64)
            self.append({'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'one'})
            session = LocalSession(session_id='s', tool='claude-code', source_path=str(self.path), project_path=None)
            row = SimpleNamespace(session_id='s', live=True, analyst_run=False)
            with patch.object(compaction_outcomes, 'record_session', return_value=[{'closed_by': 'prompts'}]) as recorder:
                ui._record_compaction_outcomes([row], [session])
                self.path.write_bytes(self.path.read_bytes().replace(b'one', b'two'))
                ui._record_compaction_outcomes([row], [session])
                self.assertEqual(recorder.call_count, 2)
        finally:
            ui._OUTCOME_SCAN.clear()


if __name__ == '__main__':
    unittest.main()
