from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compliance_service.contracts import InvalidRequest, RunSpec, parse_batch
from compliance_service.rulebook import DEFAULT_RULEBOOK_VERSION, load_rulebook, rulebook_reference


class RulebookTests(unittest.TestCase):
    def test_current_source_hash_is_registered_as_separate_immutable_revision(self):
        current = load_rulebook("impact-1023-draft-v2")
        self.assertEqual("e3fc04ca7346bb91bd8868b24efcd42155df1ae68c2a41a5f01a63a0da1e1247", current["source"]["sha256"])
        self.assertNotEqual(current["revision"], load_rulebook("impact-1023-draft-v1")["revision"])
        self.assertEqual("impact-1023-draft-v2", current["version"])
        self.assertEqual(current["revision"], load_rulebook()["revision"])
    def test_original_cells_and_reference_result_are_preserved(self):
        baseline = load_rulebook("impact-1023-draft-v1")
        self.assertEqual("impact-1023-draft-v1", baseline["version"])
        self.assertEqual("development-draft", baseline["status"])
        self.assertEqual("6c0fe47c266d5af9eeae41953c7dc1fc4b685620ee8fdcbc2e96d3d14c27f9fa", baseline["source"]["sha256"])
        self.assertEqual({2, 3}, {check["source_row"] for check in baseline["checks"]})
        self.assertTrue(all(check["result_column_is_reference_only"] for check in baseline["checks"]))
        ref = rulebook_reference(baseline, "relief_effects")
        self.assertIn("AI深度监察!F3", ref["source_cells"])
        self.assertIn("J3", baseline["checks"][1]["cells"])

    def test_request_version_is_pinned_without_reading_workbook(self):
        with TemporaryDirectory() as directory:
            spec = RunSpec("source", "mine", "relief_effects", "cycle", {"manifest": str(Path(directory) / "not-read-yet.json")})
            self.assertEqual(DEFAULT_RULEBOOK_VERSION, spec.rulebook_version)
            self.assertEqual(load_rulebook()["revision"], spec.rulebook_revision)
            self.assertIn("rulebook_revision", spec.to_dict())

    def test_unsupported_rulebook_cannot_be_silently_selected(self):
        with TemporaryDirectory() as directory:
            body = {"sourceSystem": "source", "mineCode": "mine", "checkCycle": "cycle",
                    "scope": {"manifest": str(Path(directory) / "manifest.json")},
                    "items": [{"itemCode": "relief_effects"}], "ruleVersion": "unregistered"}
            with self.assertRaises(InvalidRequest):
                parse_batch(body)
        with self.assertRaises(ValueError):
            load_rulebook("../unregistered")

    def test_uncovered_draft_conditions_cannot_be_reported_as_whole_item_pass(self):
        from compliance_service.rulebook import apply_draft_scope
        result = {"status": "pass", "description": "subset", "detail": "subset checks", "findings": [], "coverage_gaps": []}
        actual = apply_draft_scope(result, {"association_links": [], "facts": []}, "relief_effects")
        self.assertEqual("pending", actual["status"])
        self.assertEqual("pass", actual["implemented_condition_status"])
        self.assertFalse(actual["rule_coverage"]["full_rule_check_complete"])
        self.assertTrue(actual["rule_coverage"]["uncovered_conditions"])

    def test_confirmed_problem_remains_visible_despite_uncovered_conditions(self):
        from compliance_service.rulebook import apply_draft_scope
        result = {"status": "no_pass", "detail": "positive proof", "findings": [{"evidence_refs": ["explicit"]}]}
        actual = apply_draft_scope(result, {"association_links": [], "facts": []}, "relief_effects")
        self.assertEqual("no_pass", actual["status"])

    def test_candidate_only_association_cannot_make_a_definite_violation(self):
        from compliance_service.rulebook import apply_draft_scope
        result = {"status": "no_pass", "detail": "candidate proof", "findings": [{"evidence_refs": ["candidate"]}]}
        snapshot = {"facts": [{"fact_id": "f", "evidence_refs": ["candidate"]}],
                    "association_links": [{"fact_id": "f", "temporal_partition": "event range", "limitations": ["candidate"]}]}
        actual = apply_draft_scope(result, snapshot, "relief_effects")
        self.assertEqual("pending", actual["status"])
        self.assertEqual([], actual["findings"])
        self.assertTrue(actual["candidate_findings"])
