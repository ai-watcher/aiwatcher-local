from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from aiwatcher_cli import ui
from aiwatcher_cli.scanner import LocalSession


class ActionableReviewTests(TestCase):
    def setUp(self):
        ui._CONTEXT_REVIEW_CHECKS.clear()
        self.addCleanup(ui._CONTEXT_REVIEW_CHECKS.clear)
        self.now = datetime.now(timezone.utc)
        self.row = LocalSession(session_id='selected', tool='codex-cli', model='codex', project_path='/repo/app', updated_at=self.now, source_path='/fixture/rollout.jsonl')
        self.pressure = {'available': True, 'latest_turn_tokens': 187100, 'context_window': 400000}
        self.presence = SimpleNamespace(live=True, measurable=True, state='working', label='working')
        self.patches = [
            patch.object(ui, '_freshened_for_presence', side_effect=lambda rows: rows),
            patch.object(ui, 'presence_for_sessions', side_effect=lambda rows: [self.presence]),
            patch.object(ui, '_pressure_block', side_effect=lambda *args, **kwargs: dict(self.pressure)),
            patch.object(ui, 'recent_ambient_interventions', return_value=[]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def review(self, pressure=None):
        return ui.build_session_review(self.row, {'pressure': pressure or {}})

    def test_old_high_usage_is_optional_not_a_live_demand(self):
        self.row.updated_at -= timedelta(days=3)
        self.row.tokens_in = 208_300_000
        self.row.agent_calls = 1088
        review = self.review()
        self.assertEqual(review['status'], 'historical')
        self.assertIn('46.8%', review['observation'])
        self.assertIn('No live intervention', review['reason'])

    def test_current_half_full_context_does_not_need_intervention(self):
        self.assertEqual(self.review()['status'], 'clear')

    def test_current_limit_has_a_specific_reason_and_deliverable(self):
        self.pressure['latest_turn_tokens'] = 400000
        review = self.review()
        self.assertEqual(review['status'], 'actionable')
        self.assertIn('reaches', review['reason'])
        self.assertIn('Preview a Fresh Start brief', review['benefit'])
        self.assertIn('does not prove a new chat started', review['completion'])

    def test_stale_event_projection_cannot_override_fresh_context(self):
        stale = {'latest_turn_tokens': 399000, 'context_window': 400000, 'next_prompt_may_not_fit': True}
        self.assertEqual(self.review(stale)['status'], 'clear')
        current = {**stale, 'latest_turn_tokens': 187100}
        self.assertEqual(self.review(current)['status'], 'actionable')

    def test_unknown_context_does_not_become_a_zero_or_all_clear(self):
        self.pressure = {'available': False, 'reason': 'Window unknown'}
        review = self.review()
        self.assertEqual(review['status'], 'unknown')
        self.assertEqual(review['observation'], 'Window unknown')

    def test_waiting_request_is_the_next_step_instead_of_compaction(self):
        self.presence.state = 'waiting'
        self.presence.label = 'Waiting for approval to run Bash'
        review = self.review()
        self.assertEqual(review['status'], 'waiting')
        self.assertIn('approval', review['reason'])
        self.assertIn('does not approve', review['benefit'])

    def test_recent_loop_is_an_advisory_not_a_claim_that_loop_persists(self):
        record = {'session_id': 'selected', 'signal_kind': 'loop', 'created_at': self.now.isoformat()}
        with patch.object(ui, 'recent_ambient_interventions', return_value=[record]):
            review = self.review()
        self.assertEqual(review['status'], 'advisory')
        self.assertIn('not been reverified', review['reason'])
        self.assertIn('repeated attempts', review['benefit'])

    def test_other_session_or_expired_signal_does_not_leak_into_review(self):
        for sid, stamp in [('other', self.now), ('selected', self.now-timedelta(days=3))]:
            with self.subTest(sid=sid, stamp=stamp), patch.object(ui, 'recent_ambient_interventions', return_value=[{'session_id': sid, 'signal_kind': 'loop', 'created_at': stamp.isoformat()}]):
                self.assertEqual(self.review()['status'], 'clear')

    def test_notification_needs_a_current_relevant_trigger(self):
        base = {'session_id': 's1', 'project_full': '/repo/app', 'severity': 'critical', 'can_handoff': True}
        with patch.object(ui, '_fresh_start_project_quiet', return_value=False):
            for intervention in [{}, {'eligible': False, 'observed_at': self.now.isoformat()}, {'eligible': True, 'observed_at': (self.now-timedelta(days=2)).isoformat()}]:
                self.assertEqual(ui._fresh_start_context_candidates({'context_health': [{**base, 'intervention': intervention}]}), [])
            live = {**base, 'intervention': {'eligible': True, 'observed_at': self.now.isoformat()}}
            self.assertEqual(len(ui._fresh_start_context_candidates({'context_health': [live]})), 1)
            self.assertIn('&review=context', ui._context_review_companion_rows([live])[0]['url'])

    def test_cached_handoff_bubble_cannot_bypass_current_alert_policy(self):
        from tests.test_companion import WaitingSessionCompanionTests
        fixture = WaitingSessionCompanionTests()
        for intervention in [{}, {'eligible': False, 'observed_at': self.now.isoformat()}, {'eligible': True, 'observed_at': (self.now-timedelta(days=3)).isoformat()}]:
            bubble = {'session_id': 'old', 'project_full': '/repo/app', 'severity': 'critical', 'can_handoff': True, 'intervention': intervention}
            with patch.object(ui, 'presence_for_sessions', return_value=[]):
                state = fixture._state(fixture._summary(handoff_bubble=bubble, context_health=[bubble]), sessions=[])
            self.assertNotIn(state['state'], {'context_review', 'control_recommended'})

    def test_cleared_review_quiets_cached_warning_until_the_source_changes(self):
        self.review()
        candidate = {'session_id': 'selected', 'intervention': {'eligible': True, 'observed_at': self.now.isoformat()}}
        with patch.object(ui.os.path, 'getmtime', return_value=self.now.timestamp()):
            self.assertFalse(ui._context_intervention_current(candidate))
        with patch.object(ui.os.path, 'getmtime', return_value=self.now.timestamp()+1):
            self.assertTrue(ui._context_intervention_current(candidate))

    def test_unreadable_source_does_not_revive_a_cleared_warning(self):
        self.review()
        candidate = {'session_id': 'selected', 'intervention': {'eligible': True, 'observed_at': self.now.isoformat()}}
        with patch.object(ui.os.path, 'getmtime', side_effect=OSError('unavailable')):
            self.assertFalse(ui._context_intervention_current(candidate))

    def test_recheck_cache_is_bounded_and_keeps_live_limit_active(self):
        self.pressure['latest_turn_tokens'] = 400000
        for index in range(80):
            self.row.session_id = f's{index}'
            self.review()
        self.assertEqual(len(ui._CONTEXT_REVIEW_CHECKS), 64)
        self.assertTrue(ui._context_intervention_current({'session_id': 's79', 'intervention': {'eligible': True, 'observed_at': self.now.isoformat()}}))

    def test_confirmed_handoff_resolves_only_the_unchanged_source(self):
        self.pressure['latest_turn_tokens'] = 400000
        completion = {'status': 'completed', 'created_at': (self.now+timedelta(seconds=1)).isoformat()}
        self.assertEqual(ui.build_session_review(self.row, {}, completion=completion)['status'], 'resolved')
        self.row.updated_at += timedelta(seconds=2)
        self.assertEqual(ui.build_session_review(self.row, {}, completion=completion)['status'], 'actionable')

    def test_copied_or_possible_followup_does_not_claim_resolution(self):
        self.pressure['latest_turn_tokens'] = 400000
        for status in ['proof_pending', 'possible_followup']:
            completion = {'status': status, 'created_at': (self.now+timedelta(seconds=1)).isoformat()}
            self.assertEqual(ui.build_session_review(self.row, {}, completion=completion)['status'], 'actionable')

    def test_generic_positive_score_does_not_create_coaching_or_savings_calls(self):
        generic = {'score': 4, 'findings': ['No obvious cost or safety risk found from prompt text alone.'], 'suggested_prompt': 'Task\nOriginal request', 'risk': 'medium', 'blast': {'reasons': [{'text': 'A sensitive area is named'}]}}
        with patch.object(ui, 'analyze_prompt', return_value=generic) as analyze:
            result = ui.build_prompt_analysis(self.row, [{'prompt': 'Original request', 'turn': 1, 'cost_usd': 2, 'tool_calls': 1}])
        self.assertIsNone(result['coaching'])
        self.assertFalse(analyze.call_args.kwargs['include_estimate'])
