"""Layout variants: the same checklist is laid out differently per mine."""

import unittest

from compliance_service.canonical import document_from_blocks
from compliance_service.variants import (bind_formula_table, header_signature, is_formula_cell,
                                         match_variant, register_variant, similarity)


def make_doc(rows, page=1):
    html = "<table>" + "".join("<tr>" + "".join("<td>%s</td>" % cell for cell in row) + "</tr>"
                            for row in rows) + "</table>"
    return document_from_blocks([{"label": "table", "content": html}], file_id="f1", page=page)


class VariantTests(unittest.TestCase):
    def test_two_real_threshold_layouts_match_their_own_variants(self):
        hujiahe, = make_doc([["应力在线预警模型", "掘进工作面", "蓝", "黄", "橙", "红"],
                             ["浅基点", "/", "/", "≥12", "≥14"]]).tables()
        wenjiapo, = make_doc([["危险状态", "掘进工作面", "回采工作面"],
                              ["a", "1、 $ E_{\\text{p}} $", "1、 $ E_{\\text{p}} $"]]).tables()
        self.assertEqual("hujiahe_threshold_grade",
                         match_variant(hujiahe, "threshold")["variant"].variant_id)
        self.assertEqual("wenjiapo_threshold_formula",
                         match_variant(wenjiapo, "threshold")["variant"].variant_id)

    def test_unknown_layout_does_not_match(self):
        table, = make_doc([["完全不同的列A", "完全不同的列B"], ["x", "y"]]).tables()
        self.assertIsNone(match_variant(table, "threshold"))

    def test_signature_and_similarity(self):
        table, = make_doc([["等级", "预警指标", "采取措施"], ["a", "dev<25%", "正常"]]).tables()
        self.assertEqual(("等级", "预警指标", "采取措施"), header_signature(table))
        self.assertAlmostEqual(1.0, similarity(("a", "b"), ("a", "b")))
        self.assertAlmostEqual(0.0, similarity(("a",), ("b",)))

    def test_formula_cells_are_kept_as_text_not_numbers(self):
        table, = make_doc([["危险状态", "掘进工作面", "回采工作面"],
                           ["a", "1、 $ E_{\\text{p}} $", "1、 $ 5 \\times 10^{4} $"]]).tables()
        entries = bind_formula_table(table, "threshold")
        self.assertEqual(2, len(entries))
        self.assertTrue(all(entry["formula"] for entry in entries))
        self.assertTrue(all(entry["quantity"] is None for entry in entries))
        self.assertTrue(is_formula_cell("25%≤dev<100%"))
        self.assertFalse(is_formula_cell("正常生产"))

    def test_formula_variant_does_not_leak_into_unrelated_groups(self):
        # 审计发现：group="*" 让公式读法把条件证据灌进了 analysis 组
        from compliance_service.table_schema import read_table
        table, = make_doc([["危险状态", "掘进工作面", "回采工作面"],
                           ["a", "1、 E 公式", "1、 5 倍公式"]]).tables()
        self.assertTrue(read_table(table, "threshold"))
        self.assertEqual([], read_table(table, "analysis"))

    def test_column_variant_may_apply_to_any_group(self):
        from compliance_service.table_schema import read_table
        table, = make_doc([["序号", "采掘类别", "盘区名称", "工作面名称", "班次", "班长"],
                           ["1", "综掘", "42盘区", "4208运顺", "夜班", "张少锋"]]).tables()
        # 没有本组专有列时不应产出事实；prevention_daily 自己声明了 workface，
        # 因此它允许只绑定工作面的记录，其他组不行。
        self.assertEqual([], read_table(table, "analysis"))
        self.assertEqual([], read_table(table, "dispatch_daily"))
        self.assertEqual(["workface"], [entry["field"] for entry in read_table(table, "prevention_daily")])

    def test_register_variant_produces_stable_signature(self):
        table, = make_doc([["等级", "预警指标", "采取措施"], ["a", "x", "y"]]).tables()
        variant = register_variant(table, "threshold", "formula_table", mine_code="WJP")
        self.assertEqual(("等级", "预警指标", "采取措施"), variant.header_signature)
        self.assertIn("wjp", variant.variant_id)


if __name__ == "__main__":
    unittest.main()
