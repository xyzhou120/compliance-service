"""Instructions and conflicting versions cannot prove physical compliance."""
from copy import deepcopy
import unittest

from compliance_service.daily_clues import extract_daily_clues
from compliance_service.fact_extraction import extract_facts
from compliance_service.rules import evaluate_documents
from compliance_service.scope_checks import evaluate_scope
from tests.test_rules import effect_chain, run_snapshot, stop_chain
from tests.test_scope_checks import full_scope_chain


class EvidenceBoundaryTests(unittest.TestCase):
    def test_release_document_actions_keep_instruction_layer(self):
        evidence = {"evidence_id": "notice", "file_id": "notice", "kind": "paragraph",
                    "source_role": "actual_record", "material_group": "release",
                    "text": "事件编号:E1；工作面:W1；时间:2026-10-01T08:01:00；动作:已撤人"}
        fact, = extract_facts([evidence])["facts"]
        self.assertEqual("instruction", fact["fields"]["action_layer"])

    def test_arrangement_is_recognized_without_claiming_execution(self):
        evidence = {"evidence_id": "a", "file_id": "a", "source_role": "actual_record", "metadata": {"recognition_quality": "native"},
                    "text": "2026-10-01 403105工作面已安排撤人，已完成卸压施工"}
        arrangements = extract_daily_clues([evidence], [])['arrangements']
        self.assertEqual("arranged", arrangements[0]['state'])
        self.assertEqual("instruction", arrangements[0]['action_layer'])
        self.assertEqual("execution", arrangements[1]['action_layer'])

    def test_instruction_cannot_satisfy_physical_actions_or_scope(self):
        for name in ("evacuate", "relief"):
            chain = full_scope_chain()
            next(f for f in chain if f['fact_id'] == name)['fields']['action_layer'] = 'instruction'
            run, snapshot = run_snapshot('stop_work_unload', chain)
            result = evaluate_documents(run, snapshot)
            self.assertNotEqual('pass', result['status'])
            self.assertEqual('missing', next(p for p in result['propositions'] if p['code'] == name)['state'])
            scope = evaluate_scope(run, snapshot)
            self.assertFalse(any(p['state'] == 'true' and p['code'] == ('personnel_and_equipment' if name == 'evacuate' else 'relief_parameters_acceptance') for p in scope['propositions']))

    def test_dispatch_release_is_valid_as_an_order(self):
        chain = effect_chain()
        next(f for f in chain if f['fact_id'] == 'dispatch_release')['fields']['action_layer'] = 'instruction'
        run, snapshot = run_snapshot('relief_effects', chain)
        self.assertEqual('pass', evaluate_documents(run, snapshot)['status'])

    def conflicted_snapshot(self, *, same_bytes=False, event_conflict=False):
        chain = stop_chain()
        next(f for f in chain if f['fact_id'] == 'evacuate')['fields']['route_conforms'] = False
        run, snapshot = run_snapshot('stop_work_unload', chain)
        target = 'event' if event_conflict else 'evacuate'
        snapshot['files'].extend([
            {'file_id': 'version-a', 'report_date': '2026-10-01', 'material_group': 'dispatch_daily', 'content_hash': 'v1'},
            {'file_id': 'version-b', 'report_date': '2026-10-01', 'material_group': 'dispatch_daily', 'content_hash': 'v1' if same_bytes else 'v2'},
        ])
        evidence = next(e for e in snapshot['evidence'] if e['evidence_id'] == target)
        evidence.update(file_id='version-a', content_hash='v1')
        return run, snapshot

    def test_conflicting_source_violation_is_quarantined(self):
        run, snapshot = self.conflicted_snapshot()
        result = evaluate_documents(run, snapshot)
        self.assertEqual('unverifiable', result['status'])
        self.assertFalse(result['findings'])
        self.assertTrue(result['candidate_findings'])
        self.assertTrue(result['version_conflicts'])
        self.assertEqual('missing', next(p for p in result['propositions'] if p['code'] == 'evacuate')['state'])

    def test_conflicting_trigger_quarantines_dependent_findings(self):
        run, snapshot = self.conflicted_snapshot(event_conflict=True)
        result = evaluate_documents(run, snapshot)
        self.assertEqual('unverifiable', result['status'])
        self.assertFalse(result['findings'])

    def test_independent_violation_survives_version_conflict(self):
        run, snapshot = self.conflicted_snapshot()
        next(f for f in snapshot['facts'] if f['fact_id'] == 'power_off')['fields']['immediate'] = False
        result = evaluate_documents(run, snapshot)
        self.assertEqual('no_pass', result['status'])
        self.assertEqual({'power_off_immediate'}, {p['code'] for p in result['findings']})

    def test_duplicate_bytes_do_not_quarantine_a_conclusive_violation(self):
        run, snapshot = self.conflicted_snapshot(same_bytes=True)
        self.assertEqual('no_pass', evaluate_documents(run, snapshot)['status'])

    def test_gate_also_quarantines_scope_findings_after_merge(self):
        from compliance_service.rules import gate_conflicted_evidence
        _, snapshot = self.conflicted_snapshot()
        result = {'status': 'no_pass', 'description': 'physical violation', 'detail': 'detail',
                  'findings': [{'event_id': 'E1', 'workface': 'W1', 'code': 'scope', 'state': 'false', 'evidence_refs': ['evacuate']}],
                  'propositions': [{'event_id': 'E1', 'workface': 'W1', 'code': 'scope', 'state': 'false', 'evidence_refs': ['evacuate']}]}
        before = deepcopy(snapshot)
        gated = gate_conflicted_evidence(result, snapshot)
        self.assertEqual('unverifiable', gated['status'])
        self.assertEqual(before, snapshot)
        self.assertEqual(gated, gate_conflicted_evidence(deepcopy(gated), snapshot))


if __name__ == '__main__':
    unittest.main()
