import unittest

from compliance_service.service import InspectionService


class FactChainTests(unittest.TestCase):
    def snapshot(self):
        return {
            "files": [{"file_id": "daily", "original_name": "调度日报.xlsx"}],
            "evidence": [{"evidence_id": "row", "file_id": "daily", "content_hash": "version-1",
                          "locator": {"sheet": "日报", "row": 4}, "text": "401104工作面 正常生产",
                          "source_role": "actual_record"}],
            "facts": [{"fact_id": "work", "kind": "action", "event_id": None,
                       "workface": "401104工作面", "time": "2026-10-14", "evidence_refs": ["row"],
                       "fields": {"action": "continue_work", "status": "done"}}],
            "coverage_gaps": ["unassociated execution fact: work"], "association_links": [],
        }

    def test_unbound_work_remains_traceable_without_inventing_an_event(self):
        snapshot = self.snapshot()
        chains = InspectionService._fact_evidence_chains(snapshot)
        self.assertEqual(1, len(chains))
        self.assertEqual("unbound", chains[0]["event_binding"])
        self.assertIsNone(chains[0]["event_id"])
        self.assertIn("unassociated execution fact: work", chains[0]["binding_gaps"])
        self.assertEqual("调度日报.xlsx", chains[0]["sources"][0]["original_name"])
        self.assertEqual({"sheet": "日报", "row": 4}, chains[0]["sources"][0]["locator"])
        self.assertEqual("401104工作面 正常生产", chains[0]["sources"][0]["quote"])
        self.assertIsNone(snapshot["facts"][0]["event_id"])

    def test_temporal_candidate_is_not_reported_as_a_verified_binding(self):
        snapshot = self.snapshot()
        snapshot["facts"][0]["event_id"] = "event-1"
        snapshot["facts"].append({"fact_id": "trigger", "kind": "event", "event_id": "event-1",
                                  "workface": "401104工作面", "time": "2026-10-14T08:00:00",
                                  "evidence_refs": ["row"], "fields": {"trigger_type": "warning"}})
        snapshot["association_links"] = [{"fact_id": "work", "event_id": "event-1",
                                             "temporal_partition": "[event_time,next_event_time)"}]
        chain = InspectionService._fact_evidence_chains(snapshot)[0]
        self.assertEqual("candidate", chain["event_binding"])
        self.assertEqual(snapshot["association_links"], chain["association_links"])
