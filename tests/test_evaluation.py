import unittest

from compliance_service.contracts import InvalidRequest
from compliance_service.evaluation import summarize_cases


class EvaluationTests(unittest.TestCase):
    def test_fallback_is_not_counted_as_a_correct_business_decision(self):
        cases = [{"case_id": "violation", "expected_status": "no_pass", "result": {"status": "pending"},
                  "origin": "synthetic_document"},
                 {"case_id": "gap", "expected_status": "pending", "result": {"status": "pending"},
                  "origin": "synthetic_document"}]
        report = summarize_cases(cases)
        self.assertEqual(0.5, report["label_accuracy"])
        self.assertEqual(0, report["business_decision_accuracy"])
        self.assertEqual(1, report["business_decision_denominator"])
        self.assertEqual(1, report["missed_violation_count"])
        self.assertEqual(0, report["determinate_rate"])
        self.assertFalse(report["production_accuracy_validated"])

    def test_unsafe_pass_and_unregistered_evidence_are_visible(self):
        report = summarize_cases([{"case_id": "bad", "expected_status": "unverifiable",
                                   "result": {"status": "pass", "evidence_refs": ["foreign"]},
                                   "snapshot": {"evidence": []}, "origin": "synthetic_document"}])
        self.assertEqual(1, report["unsafe_pass_count"])
        self.assertEqual(1, report["untraceable_case_count"])

    def test_no_implicit_gold_or_empty_denominator_accuracy(self):
        with self.assertRaises(InvalidRequest):
            summarize_cases([{"case_id": "missing-gold", "result": {"status": "pass"}}])
        self.assertIsNone(summarize_cases([])["label_accuracy"])
