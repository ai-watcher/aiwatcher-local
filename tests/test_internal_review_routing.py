from __future__ import annotations
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch
from aiwatcher_cli import scanner, ui
from aiwatcher_cli.scanner import LocalSession, LocalEvent
from aiwatcher_cli.session_presence import SessionPresence, presence_for_sessions
from aiwatcher_cli.outcome_evidence import OutcomeEvidence


class InternalReviewRoutingTests(unittest.TestCase):
    def session(self, **extra):
        data=dict(session_id='child', tool='codex-cli', model='codex-auto-review',
                  updated_at=datetime.now(timezone.utc)-timedelta(seconds=10),
                  source_path='/fixture/child.jsonl', project_path='/repo/app',
                  session_role='approval_review', role_source='codex_session_meta', parent_session_id='parent')
        data.update(extra)
        return LocalSession(**data)

    def test_host_header_classifies_guardian_and_preserves_parent_without_prompt_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'role.jsonl'
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','source':{'subagent':{'other':'guardian'}},'parent_thread_id':'parent'}})+'\n')
            self.assertEqual(scanner._codex_role_metadata(p,'child'),('approval_review','codex_session_meta','parent'))
            self.assertEqual(scanner._codex_role_metadata(p,'other'),(None,None,None))
            p.write_text(json.dumps({'type':'response_item','payload':{'source':{'subagent':{'other':'guardian'}}}})+'\n')
            self.assertEqual(scanner._codex_role_metadata(p,'child'),(None,None,None))

    def test_ordinary_review_child_and_model_name_alone_do_not_become_internal(self):
        self.assertFalse(self.session(session_role=None,role_source=None,title='Guardian review').internal_review)
        for source in [{'subagent':{'other':'review'}},{'subagent':'review'},'desktop']:
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'role.jsonl';p.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','source':source,'parent_thread_id':'parent'}})+'\n')
                role,provenance,parent=scanner._codex_role_metadata(p,'child')
                self.assertIsNone(role)
                self.assertEqual(parent,'parent')
        self.assertFalse(self.session(tool='claude-code').internal_review)

    def test_metadata_read_is_bounded_and_malformed_or_large_headers_stay_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'role.jsonl'
            for raw in [b'not-json\n',b'[]\n',b' '*65537+b'\n']:
                p.write_bytes(raw)
                self.assertEqual(scanner._codex_role_metadata(p,'child'),(None,None,None))

    def test_role_survives_window_tail_incremental_scan_and_snapshot_round_trip(self):
        now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'rollout.jsonl'
            meta={'type':'session_meta','timestamp':(now-timedelta(days=30)).isoformat(),'payload':{'id':'child','source':{'subagent':{'other':'guardian'}},'parent_thread_id':'parent'}}
            turn={'timestamp':now.isoformat(),'type':'turn_context','payload':{'model':'codex-auto-review'}}
            usage={'timestamp':now.isoformat(),'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':82400,'output_tokens':20,'total_tokens':82420},'last_token_usage':{'input_tokens':82400,'output_tokens':20,'total_tokens':82420}}}}
            p.write_text('\n'.join(map(json.dumps,[meta,turn,usage]))+'\n')
            # Filename identity must match session_meta during a bounded tail scan.
            p2=p.with_name('rollout-00000000-0000-0000-0000-000000000001.jsonl');p.rename(p2)
            meta['payload']['id']='00000000-0000-0000-0000-000000000001'
            p2.write_text('\n'.join(map(json.dumps,[meta,turn,usage]))+'\n')
            with patch.object(scanner,'CODEX_SESSIONS_DIRS',[Path(tmp)]), patch.object(scanner,'CODEX_ROLLOUT_CACHE',None),patch.object(scanner,'CODEX_ROLLOUT_CACHE_SINCE',None):
                rows,_=scanner.scan_codex_rollouts(since=now-timedelta(days=1))
                self.assertTrue(rows[0].internal_review)
                with p2.open('a') as f:f.write(json.dumps({'timestamp':now.isoformat(),'type':'event_msg','payload':{}})+'\n')
                rows,_=scanner.scan_codex_rollouts(since=now-timedelta(days=1))
                self.assertTrue(rows[0].internal_review)
                restored=ui._session_from_json(rows[0].to_json(resolve_identity=False))
                self.assertTrue(restored.internal_review)
                self.assertEqual(restored.parent_session_id,'parent')

    def test_internal_runs_do_not_enter_quiet_notice_or_default_working_status(self):
        row=self.session()
        presence=presence_for_sessions([row],waiting={})
        self.assertTrue(presence[0].internal_review)
        with patch.object(ui,'_PRESENCE_LAST_STATES',{'child':'working'}), patch.object(ui,'_FINISHED_NOTICES',{}):
            presence[0]=replace(presence[0], state='quiet')
            ui._update_finished_notices(presence)
            self.assertEqual(ui._finished_rows(presence),[])
        block=ui._presence_block(presence)
        self.assertEqual(block['working'],0)
        self.assertEqual(block['internal_reviews'],1)

    def test_genuine_child_work_remains_in_activity_and_can_ask_for_outcome(self):
        row=self.session(session_role=None)
        presence=presence_for_sessions([row],waiting={})
        self.assertFalse(presence[0].internal_review)
        with patch.object(ui,'_PRESENCE_LAST_STATES',{'child':'working'}),patch.object(ui,'_FINISHED_NOTICES',{}),patch.object(ui,'companion_skip_active',return_value=False):
            presence[0]=replace(presence[0], state='quiet');ui._update_finished_notices(presence)
            self.assertEqual(len(ui._finished_rows(presence)),1)

    def test_internal_review_has_no_developer_outcome_or_handoff_action(self):
        row=self.session(tokens_in=900000,agent_calls=500)
        attachment=SimpleNamespace(action_label='Open Codex',available=True,reason='App focus only')
        self.assertEqual([x['id'] for x in ui.session_actions(row,attachment=attachment)],['open_tool'])
        with patch.object(ui,'_freshened_for_presence',side_effect=lambda r:r),patch.object(ui,'session_waiting_signals',return_value={}),patch.object(ui,'_pressure_block',return_value={'available':False,'reason':'unavailable'}),patch.object(ui,'recent_ambient_interventions',return_value=[]),patch.object(ui,'_cached_session_rows',return_value=[]):
            result=ui.build_session_review(row,{})
        self.assertEqual(result['status'],'internal')
        self.assertIsNone(result['parent_session_id'])

    def test_explicit_waiting_request_is_not_hidden_by_internal_role(self):
        from tests.test_companion import WaitingSessionCompanionTests
        row=self.session(updated_at=datetime.now(timezone.utc)-timedelta(minutes=3))
        fixture=WaitingSessionCompanionTests()
        signal=fixture._signal('child',minutes_ago=2)
        state=fixture._state(fixture._summary(),sessions=[row],signals=signal)
        self.assertEqual(state['state'],'session_waiting')

    def test_parent_link_requires_metadata_and_one_indexed_same_tool_parent(self):
        row=self.session();parent=LocalSession(session_id='parent',tool='codex-cli')
        with patch.object(ui,'_cached_session_rows',return_value=[parent]):
            self.assertEqual(ui._internal_review_descriptor(row)['parent_session_id'],'parent')
        for choices in [[],[LocalSession(session_id='parent',tool='claude-code')],[parent,parent]]:
            with patch.object(ui,'_cached_session_rows',return_value=choices):
                self.assertIsNone(ui._internal_review_descriptor(row)['parent_session_id'])

    def test_unknown_prices_are_unavailable_on_session_and_event_payloads(self):
        result=ui._price_presentation('codex-auto-review',0)
        self.assertIsNone(result['api_value_usd'])
        self.assertEqual(result['price_status'],'unavailable')
        self.assertNotIn('$0',result['api_value'])
        event=ui.event_json(LocalEvent(event_id='e',event_type='model_usage',session_id='child',tool='codex-cli',model='codex-auto-review',tokens_in=82400))
        self.assertFalse(event['price_available'])
        self.assertIsNone(event['api_value_usd'])
        self.assertEqual(ui._price_presentation('gpt-5.6-sol',0)['api_value'],'$0.00')
        partial=ui._price_presentation('gpt-5.6-sol',5,{'gpt-5.6-sol':{},'codex-auto-review':{}})
        self.assertEqual(partial['price_status'],'partial')
        self.assertIn('subtotal',partial['api_value'])

    def test_workspace_changes_have_an_explicit_unattributed_scope(self):
        evidence=OutcomeEvidence(session_id='child',project_path='/repo/app')
        self.assertEqual(evidence.to_json()['changed_files_scope'],'workspace_snapshot')

    def test_quiet_inspection_is_optional_and_does_not_replace_delivery_review(self):
        row=SessionPresence(session_id='s',tool='codex-cli',state='quiet',label='quiet',measurable=True)
        with patch.object(ui,'_FINISHED_NOTICES',{'s':1}),patch.object(ui,'companion_skip_active',return_value=False):
            payload=ui._finished_companion_rows([(1,row)])[0]
        self.assertEqual(ui._activity_action(payload,fallback_label='Inspect')['label'],'Inspect')
        self.assertEqual(ui._activity_action({'kind':'delivery_confirmed'},fallback_label='Review')['label'],'Review')
