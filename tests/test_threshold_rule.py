"""Document-declared graded threshold rules (per-mine standards are unknowable in advance)."""

import unittest

from compliance_service.threshold_rule import apply_rule, extract_grade_rule


class GradeRuleTests(unittest.TestCase):
    def test_above_wording_gives_baseline_and_immediate_grades(self):
        rule = extract_grade_rule(["注：监测值达到 c 及以上需立即撤人卸压"])
        self.assertEqual("declared", rule["status"])
        self.assertEqual("c", rule["baseline_grade"])
        self.assertEqual(["c", "d"], rule["immediate_grades"])
        self.assertIn("撤人", rule["evidence"])

    def test_baseline_wording_only(self):
        rule = extract_grade_rule(["本矿以 b 为基准，其余按表执行"])
        self.assertEqual("b", rule["baseline_grade"])
        self.assertEqual(["b", "c", "d"], rule["immediate_grades"])

    def test_range_wording(self):
        rule = extract_grade_rule(["监测落在 c/d 档即超阈值，需立即撤人和卸压"])
        self.assertEqual("c", rule["baseline_grade"])
        self.assertEqual(["c", "d"], rule["immediate_grades"])

    def test_unreadable_document_stays_unresolved(self):
        rule = extract_grade_rule(["微震监测统计 总频次 69 最大能量 4.92×10^3"])
        self.assertEqual("unresolved", rule["status"])
        self.assertIsNone(rule["baseline_grade"])
        self.assertEqual([], rule["immediate_grades"])

    def test_document_rule_overrides_variant_default(self):
        schema = {"baseline_grade": "c", "over_threshold_grades": ("c", "d")}
        effective = apply_rule(schema, {"baseline_grade": "b", "immediate_grades": ["b", "c"]})
        self.assertEqual("b", effective["baseline_grade"])
        self.assertEqual("document", effective["baseline_source"])
        self.assertEqual(("b", "c"), effective["over_threshold_grades"])
        fallback = apply_rule(schema, {"status": "unresolved", "baseline_grade": None})
        self.assertEqual("variant", fallback["baseline_source"])
        unset = apply_rule({}, {"baseline_grade": None})
        self.assertEqual("unset", unset["baseline_source"])


if __name__ == "__main__":
    unittest.main()
