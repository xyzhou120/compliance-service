"""Risk grade mapping and grade-matrix threshold selection."""

import unittest

from compliance_service.grades import better_grade, normalize_risk, select_for_risk


class GradeMappingTests(unittest.TestCase):
    def test_document_wording_maps_to_grade(self):
        self.assertEqual("红", normalize_risk("强冲击危险"))
        self.assertEqual("橙", normalize_risk("中等冲击危险区域"))
        self.assertEqual("黄", normalize_risk("弱"))
        self.assertEqual("蓝", normalize_risk("无冲击危险"))
        self.assertEqual("红", normalize_risk("红"))
        self.assertIsNone(normalize_risk(""))
        self.assertIsNone(normalize_risk("无法判定"))

    def test_more_severe_grade_wins(self):
        self.assertEqual("红", better_grade("黄", "红"))
        self.assertEqual("橙", better_grade(None, "橙"))
        self.assertEqual("蓝", better_grade("蓝", None))

    def test_threshold_selection_never_guesses(self):
        thresholds = [{"grade": "蓝", "value": "3"}, {"grade": "红", "value": "12"}]
        chosen, diag = select_for_risk(thresholds, "强冲击危险")
        self.assertEqual([{"grade": "红", "value": "12"}], chosen)
        self.assertIsNone(diag["reason"])
        chosen, diag = select_for_risk(thresholds, "")
        self.assertEqual([], chosen)
        self.assertEqual("RISK_GRADE_UNKNOWN", diag["reason"])
        chosen, diag = select_for_risk(thresholds, "橙")
        self.assertEqual([], chosen)
        self.assertEqual("NO_THRESHOLD_FOR_GRADE", diag["reason"])
        chosen, diag = select_for_risk([{"value": "5"}], "强冲击危险")
        self.assertEqual([{"value": "5"}], chosen)
        self.assertEqual("NO_GRADED_THRESHOLDS", diag["reason"])


if __name__ == "__main__":
    unittest.main()
