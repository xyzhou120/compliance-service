"""Grade-matrix table schemas (real 临界指标 shape)."""

import unittest

from compliance_service.canonical import document_from_blocks
from compliance_service.table_schema import (SCHEMAS, bind_schema, facts_for_document,
                                             facts_from_schema, match_schema)


def table_html(rows):
    return "<table>" + "".join(
        "<tr>" + "".join("<td>%s</td>" % cell for cell in row) + "</tr>" for row in rows) + "</table>"


GRADE_ROWS = [
    ["应力在线预警模型", "掘进工作面", "蓝", "黄", "橙", "红"],
    ["总频次", "≤3", "4~6", "7~9", "≥10"],
    ["最大能量/J", "≤1.0×10^3", "1.0~2.0×10^3", "2.0~3.0×10^3", "≥3.0×10^3"],
]


def doc(rows=GRADE_ROWS, page=2):
    return document_from_blocks([{"label": "table", "content": table_html(rows)}],
                                file_id="f1", page=page)


class SchemaTests(unittest.TestCase):
    def test_grade_matrix_is_recognised_and_bound(self):
        table, = doc().tables()
        match = match_schema(table, "冲击危险性预警临界指标")
        self.assertIsNotNone(match)
        self.assertEqual("threshold_color_grade", match["schema"]["schema_id"])
        self.assertEqual(["蓝", "黄", "橙", "红"], [grade for _, grade in match["grade_columns"]])
        bound = bind_schema(table, match["schema"])
        self.assertEqual(8, len(bound))                       # 2 数据行 × 4 色阶
        first = bound[0]
        self.assertEqual(("总频次", "蓝", "≤3"), (first["label"], first["grade"], first["raw"]))
        self.assertEqual({"蓝", "黄", "橙", "红"}, {entry["grade"] for entry in bound})
        self.assertIn("locator", first)

    def test_plain_field_table_is_not_forced_into_a_schema(self):
        table, = doc([["方法", "实测值", "单位"], ["应力监测法", "10", "MPa"]]).tables()
        self.assertIsNone(match_schema(table, ""))

    def test_facts_carry_grade_and_unit(self):
        table, = doc([["预警模型", "蓝", "黄", "橙", "红"],
                      ["最大能量", "≤1.0×10^3", "≤1.5×10^3", "≤2.0×10^3", "≥3.0×10^3"]]).tables()
        facts = facts_from_schema(table, SCHEMAS[0], title="临界指标")
        self.assertEqual(4, len(facts))
        self.assertEqual("threshold", facts[0]["kind"])
        self.assertEqual("requirement", facts[0]["source_role"])
        self.assertEqual("蓝", facts[0]["fields"]["grade"])
        self.assertEqual("最大能量", facts[0]["fields"]["metric"])
        self.assertTrue(facts[0]["evidence_refs"])

    def test_placeholder_cells_are_not_emitted_as_values(self):
        table, = doc([["预警模型", "蓝", "黄", "橙", "红"],
                      ["浅基点", "/", "/", "≥12", "≥14"]]).tables()
        facts = facts_from_schema(table, SCHEMAS[0], title="临界指标")
        self.assertEqual(["橙", "红"], [fact["fields"]["grade"] for fact in facts])
        self.assertEqual("≥12", facts[0]["fields"]["value"])

    def test_group_reader_dispatch_covers_all_modes(self):
        from compliance_service.table_schema import (GROUP_READERS, read_table, reader_choice,
                                                     reader_for)
        self.assertEqual(9, len(GROUP_READERS))
        self.assertEqual("grade_matrix", reader_for("threshold"))
        self.assertEqual("label_value", reader_for("design"))
        self.assertEqual("field_columns", reader_for("dispatch_daily"))
        grade_table, = doc().tables()
        self.assertTrue(read_table(grade_table, "threshold", title="冲击危险性预警临界指标"))
        self.assertEqual("hujiahe_threshold_grade",
                         reader_choice(grade_table, "threshold")["variant_id"])
        formula, = doc([["危险状态", "掘进工作面", "回采工作面"],
                        ["a", "1、 E 公式", "1、 5 倍公式"]]).tables()
        entries = read_table(formula, "threshold")
        self.assertEqual(2, len(entries))
        self.assertEqual({"condition"}, {entry["field"] for entry in entries})
        self.assertEqual("wenjiapo_threshold_formula", reader_choice(formula, "threshold")["variant_id"])
        two_col, = doc([["工作面", "W1"], ["技术负责人", "张工"], ["动作", "停工"]]).tables()
        entries = read_table(two_col, "design")
        # design 组只接受本组字段：动作不属于设计资料，不能被绑进来
        self.assertEqual({"workface", "technical_head"}, {entry["field"] for entry in entries})
        field_table, = doc([["工作面", "动作", "状态"], ["W1", "停工", "已完成"]]).tables()
        dispatch = read_table(field_table, "dispatch_daily")
        self.assertIn("action", {entry["field"] for entry in dispatch})

    def test_lettered_grades_use_the_configured_baseline(self):
        from compliance_service.table_schema import SCHEMAS, facts_from_schema
        level_schema = next(schema for schema in SCHEMAS
                            if schema["schema_id"] == "warning_level_abc")
        table, = doc([["综合预警等级", "微震", "a", "b", "c", "d"],
                      ["事件次数", "≤3", "4~6", "7~9", "≥10"]]).tables()
        facts = facts_from_schema(table, level_schema, title="综合预警指标")
        by_grade = {fact["fields"]["grade"]: fact["fields"] for fact in facts}
        self.assertEqual(["a", "b", "c", "d"], sorted(by_grade))
        self.assertFalse(by_grade["a"]["over_threshold"])
        self.assertFalse(by_grade["b"]["over_threshold"])
        # 业务口径：c 为基准，c/d 都算超阈值且需要立即撤人卸压
        self.assertTrue(by_grade["c"]["over_threshold"])
        self.assertTrue(by_grade["d"]["over_threshold"])
        self.assertTrue(by_grade["c"]["immediate_action_required"])
        self.assertNotIn("immediate_action_required", by_grade["a"])

    def test_attendance_and_cutting_columns_bind_for_dispatch(self):
        table, = doc([["序号", "采掘类别", "盘区名称", "工作面名称", "班次", "班长",
                       "出勤人数（人）", "当班割煤刀数（刀）", "日计划割煤（刀）", "本月累计割煤（刀）"],
                      ["1", "综掘", "42盘区", "4208运顺", "夜班", "张少锋", "18", "2.7", "5", "120"]]).tables()
        from compliance_service.table_schema import read_table
        entries = {entry["field"]: entry["raw"] for entry in read_table(table, "dispatch_daily")}
        self.assertEqual("4208运顺", entries["workface"])
        self.assertEqual("18", entries["attendance_count"])
        self.assertEqual("2.7", entries["coal_cutting_count"])
        self.assertEqual("5", entries["planned_cutting_count"])
        self.assertEqual("120", entries["cumulative_cutting_count"])

    def test_row_oriented_grade_matrix(self):
        # 文家坡：等级在行上（a/b/c × 微震/地音/应力），不是列上
        from compliance_service.table_schema import SCHEMAS, facts_from_schema
        level_schema = next(schema for schema in SCHEMAS
                            if schema["schema_id"] == "warning_level_abc")
        table, = doc([["等级", "微震", "地音", "应力"],
                      ["a", "≤3", "≤2", "≤1"],
                      ["c", "7~9", "6~8", "≥5"],
                      ["d", "≥10", "≥9", "≥7"]]).tables()
        facts = facts_from_schema(table, level_schema, title="综合预警指标")
        self.assertEqual(9, len(facts))
        c_measured = next(fact for fact in facts
                          if fact["fields"]["grade"] == "c" and "微震" in fact["fields"]["metric"])
        self.assertTrue(c_measured["fields"]["over_threshold"])
        self.assertTrue(c_measured["fields"]["immediate_action_required"])
        a_measured = next(fact for fact in facts
                          if fact["fields"]["grade"] == "a" and "微震" in fact["fields"]["metric"])
        self.assertFalse(a_measured["fields"]["over_threshold"])

    def test_document_level_application(self):
        facts = facts_for_document(doc(), titles={"p2-t1": "冲击危险性预警临界指标"})
        self.assertEqual(8, len(facts))
        self.assertEqual({"threshold_color_grade"}, {fact["schema_id"] for fact in facts})


if __name__ == "__main__":
    unittest.main()
