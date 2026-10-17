"""Regressions from the copied 12k prompt and inflated review records."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import local_state, processes, scanner, ui
from aiwatcher_cli.optimize_review import session_review_identity


class OptimizeIntegrityTests(unittest.TestCase):
    def test_long_prompt_roundtrips_with_confirmation_and_guardrails(self):
        text = 'AIWatcher AI-assisted Optimize cleanup prompt\n' + ('Evidence row\n' * 1600) + '\nConfirm the exact action before deletion.\nPreserve unfinished work.'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AIWATCHER_STATE_FILE': str(Path(folder)/'state.json')}):
            local_state.record_ai_assist_cache(workflow='optimize_cleanup', evidence_hash='long', text=text)
            self.assertEqual(local_state.ai_assist_cache_get('optimize_cleanup','long')['text'], text)

    def test_legacy_or_tampered_cache_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AIWATCHER_STATE_FILE': str(Path(folder)/'state.json')}):
            path=Path(folder)/'state.json'
            local_state.record_ai_assist_cache(workflow='optimize_cleanup', evidence_hash='legacy', text='full prompt with guardrails')
            data=json.loads(path.read_text())
            cache=next(iter(data['ai_assist_cache'].values()))
            del cache['text_sha256']
            path.write_text(json.dumps(data))
            self.assertIsNone(local_state.ai_assist_cache_get('optimize_cleanup','legacy'))

    def test_oversized_cache_fails_without_partial_storage(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AIWATCHER_STATE_FILE': str(Path(folder)/'state.json')}):
            with self.assertRaisesRegex(ValueError, 'refusing to truncate'):
                local_state.record_ai_assist_cache(workflow='optimize_cleanup', evidence_hash='huge', text='x'*(local_state.MAX_AI_ASSIST_CACHE_TEXT_CHARS+1))
            self.assertIsNone(local_state.ai_assist_cache_get('optimize_cleanup','huge'))

    def test_native_app_and_crash_helpers_are_not_orphans_from_ppid_one(self):
        for command in ['/Applications/Claude.app/Contents/MacOS/Claude',
                        '/Applications/Claude.app/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler',
                        '/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Helpers/browser_crashpad_handler']:
            with self.subTest(command=command):
                row=processes.ProcessRow(pid=1,ppid=1,state='S',age_seconds=10000,rss_kb=10000,cpu_percent=0,command=command)
                result=processes.classify_process(row)
                self.assertTrue(result is None or not result.stale)

    def test_desktop_app_with_independent_stale_signal_still_surfaces(self):
        row=processes.ProcessRow(pid=1,ppid=1,state='T',age_seconds=10000,rss_kb=10000,cpu_percent=0,command='/Applications/Claude.app/Contents/MacOS/Claude')
        self.assertIn('stopped/suspended process', processes.classify_process(row).stale_reasons)

    def test_bounded_rollout_and_database_share_canonical_id(self):
        sid='019ddc2f-59f6-7ef3-8252-45d71093e945'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            path=root/f'rollout-2026-04-29T22-19-43-{sid}.jsonl'
            rows=[{'timestamp':'2026-10-14T00:00:00Z','type':'turn_context','payload':{'cwd':folder,'model':'codex'}},
                  {'timestamp':'2026-10-14T00:00:01Z','type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12},'last_token_usage':{'input_tokens':10,'output_tokens':2}}}}]
            path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            db=root/'state.sqlite'
            con=sqlite3.connect(db)
            con.execute('CREATE TABLE threads (id TEXT,cwd TEXT,name TEXT,model TEXT,tokens_used INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER,archived INTEGER,rollout_path TEXT)')
            con.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)',(sid,folder,'Orbit-dev','codex',12,1791936000000,1791936001000,0,str(path)))
            con.commit();con.close()
            with patch.object(scanner,'CODEX_SESSIONS_DIRS',[root]),patch.object(scanner,'CODEX_DB_PATHS',[db]),patch.object(scanner,'CODEX_ROLLOUT_CACHE',None),patch.object(scanner,'CODEX_ROLLOUT_CACHE_SINCE',None):
                sessions=scanner.scan_codex_cli()
            self.assertEqual([s.session_id for s in sessions],[sid])

    def test_database_only_row_retains_rollout_identity_for_internal_filter(self):
        sid='01a137ee-1409-72e3-aef4-6807d53d626c'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/f'rollout-{sid}.jsonl';db=root/'state.sqlite'
            path.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'source':{'subagent':{'other':'guardian'}}}})+'\n')
            con=sqlite3.connect(db)
            con.execute('CREATE TABLE threads (id TEXT,cwd TEXT,name TEXT,model TEXT,tokens_used INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER,archived INTEGER,rollout_path TEXT)')
            con.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)',(sid,folder,'Guardian review','codex',10,1,2,0,str(path)))
            con.commit();con.close()
            with patch.object(scanner,'CODEX_SESSIONS_DIRS',[root]),patch.object(scanner,'CODEX_DB_PATHS',[db]),patch.object(scanner,'scan_codex_rollouts',return_value=([],[])):
                sessions=scanner.scan_codex_cli()
            self.assertEqual(session_review_identity(sessions[0],{})['session_role'],'approval_session')
