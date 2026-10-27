"""Table selection, chart gate and handwriting policy."""

import unittest

from compliance_service.canonical import document_from_blocks
from compliance_service.charts import chart_semantics, read_chart, validate_chart
from compliance_service.handwriting import classify, reject_numeric_from_handwriting
from compliance_service.tables import score_table, select_for_document, select_table


def table(*rows):
    return "<table>" + "".join("<tr>" + "".join("<td>%s</td>" % cell for cell in row) + "</tr>"
                            for row in rows) + "</table>"


class TableSelectionTests(unittest.TestCase):
    def test_field_columns_win_over_area_matrix(self):
        fields_table = table(["方法", "指标", "测点", "实测值", "单位"],
                             ["应力监测法", "煤体应力", "P1", "10", "MPa"])
        area_table = table(["全矿范围内", "403109工作面", "预警统计"],
                           ["69", "20", "24"])
        doc = document_from_blocks([{"label": "table", "content": area_table},
                                    {"label": "table", "content": fields_table}],
                                   file_id="f1", page=1)
        choice = select_table(doc, "threshold")
        self.assertIsNotNone(choice)
        self.assertEqual(5, choice["score"])
        self.assertIn("value", choice["fields"])
        table_model, payload = select_for_document(doc, "threshold")
        self.assertIsNotNone(table_model)
        self.assertEqual(choice["table_id"], table_model.table_id)

    def test_title_keywords_raise_the_score(self):
        doc = document_from_blocks([{"label": "table",
                                     "content": table(["方法", "实测值"], ["x", "1"])}],
                                   file_id="f1", page=2)
        plain = select_table(doc, "threshold")
        hinted = select_table(doc, "threshold", titles={plain["table_id"]: "冲击危险预警临界指标"})
        self.assertGreater(hinted["score"], plain["score"])
        self.assertTrue(hinted["title_hits"])

    def test_matrix_without_field_columns_is_not_selected(self):
        doc = document_from_blocks([{"label": "table",
                                     "content": table(["全矿范围内", "工作面"], ["69", "20"])}],
                                   file_id="f1", page=3)
        self.assertIsNone(select_table(doc, "threshold"))


class ChartGateTests(unittest.TestCase):
    def test_line_without_labels_is_semantic_only(self):
        payload = {"chart_type": "line", "title": "地音监测趋势图", "data_labels_present": False,
                   "x_axis": {"label": "日期", "ticks": ["1-1", "1-2", "1-3"]},
                   "y_axis": {"label": "频次", "unit": "次", "ticks": [0, 30, 60, 90]},
                   "series": [{"name": "日频次", "values": []}], "confidence": 0.75}
        result = validate_chart(payload)
        self.assertEqual("chart_semantic_only", result["status"])
        self.assertIn("NO_DATA_LABELS", result["reasons"])
        self.assertEqual("地音监测趋势图", chart_semantics(payload)["title"])

    def test_labelled_linear_chart_can_be_verified(self):
        payload = {"chart_type": "bar", "data_labels_present": True,
                   "x_axis": {"ticks": ["10-01", "10-02"]},
                   "y_axis": {"ticks": [0, 50, 100]},
                   "series": [{"name": "预警次数", "values": [20, 70]}]}
        result = validate_chart(payload)
        self.assertEqual("chart_verified", result["status"])
        self.assertEqual([20.0, 70.0], result["values"])

    def test_non_linear_or_out_of_range_charts_are_rejected(self):
        non_linear = {"data_labels_present": True, "x_axis": {"ticks": ["a", "b"]},
                      "y_axis": {"ticks": [0, 10, 40]}, "series": [{"values": [5]}]}
        self.assertEqual("chart_rejected", validate_chart(non_linear)["status"])
        outside = {"data_labels_present": True, "x_axis": {"ticks": ["a", "b"]},
                   "y_axis": {"ticks": [0, 50, 100]}, "series": [{"values": [120]}]}
        self.assertIn("VALUE_OUTSIDE_AXIS_RANGE", validate_chart(outside)["reasons"])

    def test_read_chart_uses_injected_model_call(self):
        def vlm(image, prompt):
            self.assertIn("JSON", prompt)
            return {"chart_type": "line", "data_labels_present": False,
                    "x_axis": {"ticks": ["1", "2"]}, "y_axis": {"ticks": [0, 10, 20]},
                    "series": [{"values": []}]}
        result = read_chart(vlm, b"img")
        self.assertEqual("chart_semantic_only", result["status"])


class HandwritingTests(unittest.TestCase):
    def test_handwriting_is_never_a_numeric_source(self):
        policy = classify({"handwriting_present": True, "handwriting_readable": True})
        self.assertTrue(policy["requires_review"])
        self.assertFalse(policy["numeric_allowed"])
        self.assertEqual(["HANDWRITTEN_REGION"], policy["reasons"])
        unresolved = classify({"handwriting_present": True, "handwriting_readable": False})
        self.assertIn("HANDWRITING_UNRESOLVED", unresolved["reasons"])
        clean = classify({})
        self.assertFalse(clean["requires_review"])
        self.assertFalse(reject_numeric_from_handwriting("measurement.value")["verified"])


if __name__ == "__main__":
    unittest.main()
