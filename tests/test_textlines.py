"""Text-line binding: the fallback used on photo pages with degenerate tables."""

import unittest

from compliance_service.textlines import bind_lines, facts_from_lines, lines_from_blocks


TEXT = "\n".join(["微震监测统计", "全矿范围内 403109工作面", "总频次 69", "当日推进度 4m",
                  "采面总体推进度：1380m", "方法：应力监测法", "指标 煤体应力", "测点 P1",
                  "实测值 4.92", "单位 MPa"])


def lines(text=TEXT, page=1):
    return lines_from_blocks([{"label": "ocr", "content": text}], file_id="f1", page=page)


class TextBindingTests(unittest.TestCase):
    def test_only_whole_labels_are_bound(self):
        bound = bind_lines(lines(), "prevention_daily")
        fields = {entry["field"]: entry["raw"] for entry in bound}
        self.assertEqual("应力监测法", fields["method"])
        self.assertEqual("煤体应力", fields["metric"])
        self.assertEqual("P1", fields["point"])
        self.assertEqual("4.92", fields["value"])
        self.assertEqual("MPa", fields["unit"])
        # "全矿范围内 403109工作面" 含"工作面"但不是工作面列，不得被绑定
        self.assertNotIn("workface", fields)
        # 未登记的业务列（总频次/推进度）不得凭空映射
        self.assertNotIn("status", fields)

    def test_binding_keeps_line_locators(self):
        bound = bind_lines(lines(), "prevention_daily")
        locator = bound[0]["locator"]
        self.assertEqual(("f1", 1), (locator["file_id"], locator["page"]))
        self.assertIsNotNone(locator["row"])

    def test_facts_group_one_block_and_attach_units(self):
        fact, = facts_from_lines(lines(), "prevention_daily")
        self.assertEqual("measurement", fact["kind"])
        self.assertEqual("actual_record", fact["source_role"])
        self.assertEqual("MPa", fact["fields"]["unit"])
        self.assertEqual("4.92", fact["fields"]["value"])
        self.assertTrue(fact["evidence_refs"])

    def test_threshold_group_marks_requirement_role(self):
        text = "\n".join(["方法：应力监测法", "指标 煤体应力", "测点 P1", "实测值 10", "单位 MPa",
                          "批复 已批复", "有效起始 2026-09-23", "适用范围 W1"])
        fact, = facts_from_lines(lines(text, page=2), "threshold")
        self.assertEqual("requirement", fact["source_role"])
        self.assertEqual("threshold", fact["kind"])
        self.assertEqual("已批复", fact["fields"]["approved"])

    def test_empty_or_unrelated_text_yields_nothing(self):
        self.assertEqual([], bind_lines(lines("第一段说明文字\n第二段说明文字"), "prevention_daily"))
        self.assertEqual([], facts_from_lines([], "prevention_daily"))


if __name__ == "__main__":
    unittest.main()
