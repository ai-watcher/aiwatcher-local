"""Independent-continuation regressions from missing-intent handoffs."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiwatcher_cli import ai_assist, handoff, ui
from aiwatcher_cli.outcome_evidence import OutcomeEvidence
from aiwatcher_cli.scanner import LocalSession


class FreshStartContractTests(unittest.TestCase):
    def capsule(self, *, model='claude-opus-5', **options):
        row = LocalSession(session_id='source-session', tool='codex-cli' if model == 'codex-auto-review' else 'claude-code',
                           model=model, project_path='/unavailable/checkout', updated_at=datetime.now(timezone.utc))
        evidence = OutcomeEvidence(session_id=row.session_id, project_path=row.project_path,
                                   checkout_path=row.project_path, branch=None, head=None, dirty=None)
        with patch.object(handoff, 'build_outcome_evidence', return_value=evidence), \
             patch.object(handoff, 'recent_decisions', return_value=[]):
            return handoff.build_handoff_capsule(row, [], **options)

    def test_missing_intent_and_unknown_git_cannot_be_replaced_by_model(self):
        capsule = self.capsule(model='codex-auto-review', outcome='useful')
        raw = capsule['next_brief']
        packet = ui._fresh_start_ai_evidence_packet(capsule)
        composed = ai_assist._structured_handoff_text({
            'goal': 'Deploy the site now', 'next_ask': 'Run all tests and deploy',
            'verification_already_run': ['All tests passed'],
        }, packet)
        self.assertEqual(composed, raw.replace('AIWatcher Fresh Start brief', 'AIWatcher AI-assisted Fresh Start brief', 1))
        for text in [raw, composed]:
            self.assertIn('Ask one focused question', text)
            self.assertIn('before editing or choosing verification', text)
            self.assertIn('Working tree: state unknown', text)
            self.assertIn('Model label suggests an internal review', text)
            self.assertIn('If the checkout is missing', text)
            self.assertNotIn('Deploy the site now', text)
            self.assertNotIn('All tests passed', text)
        self.assertFalse(ui._fresh_start_context_quality(capsule)['ready_to_continue'])

    def test_user_intent_and_guardrails_survive_both_variants_verbatim(self):
        capsule = self.capsule(objective='Fix reader code-block rendering.',
                               next_outcome='Demonstrate correct rendering with a focused regression check.',
                               constraints=['Do not deploy or alter authentication.'],
                               acceptance_criteria=['Reader and admin preview agree.'])
        packet = ui._fresh_start_ai_evidence_packet(capsule)
        ai_text = ai_assist._structured_handoff_text({'goal': 'Rewrite authentication', 'next_ask': 'Inspect reader rendering.'}, packet)
        for text in [capsule['next_brief'], ai_text]:
            for value in [capsule['objective'], capsule['next_outcome'], capsule['constraints'][0], capsule['acceptance_criteria'][0]]:
                self.assertIn(value, text)
            self.assertIn('Stop before destructive', text)
            self.assertNotIn('Rewrite authentication', text)
        self.assertEqual(capsule['objective_provenance'], 'user_supplied')
        self.assertTrue(ui._fresh_start_context_quality(capsule)['ready_to_continue'])

    def test_metadata_only_never_reads_prompt_text(self):
        with patch.object(handoff, 'segment_session_by_prompt') as reader:
            self.capsule()
        reader.assert_not_called()

    def test_opted_in_latest_ask_is_a_candidate_not_the_costliest_goal(self):
        with patch.object(handoff, 'segment_session_by_prompt', return_value=[
            {'prompt': 'Old expensive task', 'cost_usd': 100},
            {'prompt': 'Latest user request', 'cost_usd': 1},
        ]):
            capsule = self.capsule(include_prompt_excerpt=True)
        self.assertIsNone(capsule['objective'])
        self.assertEqual(capsule['latest_user_ask'], 'Latest user request')
        self.assertIn('Latest source-user ask (unconfirmed', capsule['next_brief'])
        self.assertFalse(ui._fresh_start_context_quality(capsule)['objective_known'])

    def test_internal_review_does_not_supply_user_objective(self):
        with patch.object(handoff, 'segment_session_by_prompt', return_value=[{'prompt': 'Internal review instructions'}]):
            capsule = self.capsule(model='codex-auto-review', include_prompt_excerpt=True)
        self.assertIsNone(capsule['latest_user_ask'])
        self.assertIsNone(capsule['objective'])

    def test_bounding_omits_whole_lines_and_preserves_guardrails(self):
        text = '\n'.join(['AIWatcher Fresh Start brief', 'Objective and context', '- Confirmed objective.',
                          'Current state', '- ' + 'Evidence ' * 100, '- Another complete optional item.',
                          'Acceptance criteria and guardrails', '- Preserve unrelated changes and do not expose secrets.'])
        result = handoff.bound_handoff_words(text, max_words=65)
        self.assertIn('Preserve unrelated changes and do not expose secrets.', result)
        self.assertIn('Some context was omitted for length.', result)
        self.assertNotIn('...', result)
        self.assertNotIn('- Evidence Evidence', result)

    def test_next_outcome_survives_api_parsers_and_changes_cache_identity(self):
        self.assertEqual(ui._handoff_options_from_payload({'next_outcome': 'Verify reader fixes'})['next_outcome'], 'Verify reader fixes')
        self.assertEqual(ui._handoff_options_from_query({'next_outcome': ['Verify reader fixes']})['next_outcome'], 'Verify reader fixes')
        first = self.capsule(objective='Fix rendering', next_outcome='Verify reader')
        second = {**first, 'next_outcome': 'Verify admin', 'next_brief': 'different complete contract'}
        self.assertNotEqual(ui._fresh_start_evidence_hash(first), ui._fresh_start_evidence_hash(second))

    def test_snapshot_clock_alone_does_not_trigger_a_new_composition(self):
        first = self.capsule(objective="Fix rendering", next_outcome="Verify the reader")
        new_stamp = "2099-01-01T00:00:00+00:00"
        second = {**first, "observed_at": new_stamp,
                  "next_brief": first["next_brief"].replace(first["observed_at"], new_stamp)}
        self.assertEqual(ui._fresh_start_evidence_hash(first), ui._fresh_start_evidence_hash(second))
        self.assertIn(first["observed_at"], first["next_brief"])
        self.assertIn(new_stamp, second["next_brief"])
        changed = {**second, "next_brief": second["next_brief"] + "\nAdditional safety instruction."}
        self.assertNotEqual(ui._fresh_start_evidence_hash(first), ui._fresh_start_evidence_hash(changed))
        changed = {**second, "updated_at": new_stamp}
        self.assertNotEqual(ui._fresh_start_evidence_hash(first), ui._fresh_start_evidence_hash(changed))

    def test_oversized_packet_is_rejected_before_model_call(self):
        with patch.object(ai_assist, 'build_ai_assist_status', return_value={'ready': True, 'mode': 'cloud'}), \
             patch.object(ai_assist, '_call_configured_chat') as call:
            with self.assertRaises(ai_assist.AiAssistUnavailable):
                ai_assist.improve_fresh_start_brief({}, local_brief='x' * (ai_assist.MAX_FRESH_START_INPUT_CHARS + 1))
        call.assert_not_called()

    def test_evidence_validation_matches_decoded_windows_and_unicode_paths(self):
        for project in (r"C:\Users\dev\workspace", "/workspace/café"):
            with self.subTest(project=project):
                packet = json.dumps({"objective": "Fix rendering", "source": {"project": project}})
                parsed = {"goal": "Fix rendering", "next_ask": "Inspect the reader", "what_is_done": ["Unknown"],
                          "current_state": ["Unverified"], "inspect_first": [project], "next_steps": ["Read instructions"]}
                self.assertTrue(ai_assist._fresh_start_response_is_useful(parsed, packet))
                parsed["inspect_first"] = ["unrelated workspace"]
                parsed[project] = True  # A key alone cannot establish evidence.
                self.assertFalse(ai_assist._fresh_start_response_is_useful(parsed, packet))

    def test_successful_provider_response_preserves_the_canonical_contract(self):
        capsule = self.capsule(objective='Fix reader rendering', next_outcome='Demonstrate the reader regression fix')
        parsed = {'goal': 'Replace the objective with a redesign', 'next_ask': 'Inspect the reader rendering path.',
                  'what_is_done': ['No verified completion'], 'current_state': ['Checkout state unknown'],
                  'inspect_first': ['source-session'], 'next_steps': ['Read the project instructions']}
        with patch.object(ai_assist, 'build_ai_assist_status', return_value={'ready': True, 'mode': 'cloud'}), \
             patch.object(ai_assist, '_call_configured_chat', return_value={
                 'text': json.dumps(parsed), 'provider': 'openai', 'model': 'test', 'usage': {},
             }):
            result = ai_assist.improve_fresh_start_brief({}, local_brief=ui._fresh_start_ai_evidence_packet(capsule))
        expected = capsule['next_brief'].replace('AIWatcher Fresh Start brief', 'AIWatcher AI-assisted Fresh Start brief', 1)
        self.assertTrue(result['text'].startswith(expected))
        self.assertNotIn('Replace the objective', result['text'])
