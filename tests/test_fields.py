"""Field archive: alias binding, unit scales and canonical fact generation."""

import unittest

from compliance_service.canonical import document_from_blocks
from compliance_service.fields import (bind_table, canonical_field, facts_from_document,
                                       GROUP_FIELDS)
from compliance_service.rules import evaluate_documents
from compliance_service.units import comparable, quantity


def doc_for(html, page=1):
    return document_from_blocks([{"label": "table", "content": html}], file_id="f1", page=page,
                                file_hash="hash-1")


def table(*rows):
    return "<table>" + "".join("<tr>" + "".join("<td>%s</td>" % cell for cell in row) + "</tr>"
                            for row in rows) + "</table>"


class BindingTests(unittest.TestCase):
    def test_aliases_map_to_canonical_fields(self):
        self.assertEqual("action", canonical_field("动作"))
        self.assertEqual("value", canonical_field("实测值"))
        self.assertEqual("unit", canonical_field("单位"))
        self.assertEqual("workface", canonical_field("工作面"))
        self.assertIsNone(canonical_field("完全无关的列"))

    def test_bind_keeps_units_and_scale_family(self):
        html = table(["工作面", "动作", "实测值", "单位"],
                     ["W1", "卸压", "8", "MPa"],
                     ["W1", "检验", "1.2", "%"])
        table_model, = doc_for(html).tables()
        bound = bind_table(table_model, "prevention_daily")
        values = {(e["field"], e["raw"]): e for e in bound if e["field"] in {"value", "unit", "action"}}
        self.assertIn(("action", "卸压"), values)
        mpa = values[("value", "8")]
        self.assertEqual("pressure", mpa["quantity"]["dimension"])
        pct = values[("value", "1.2")]
        self.assertEqual("percent", pct["quantity"]["scale"])
        self.assertFalse(comparable(quantity("1.2", "%"), quantity("1", "1")))
        self.assertTrue(comparable(quantity("8", "MPa"), quantity("8000", "kPa")))

    def test_missing_or_unknown_columns_are_not_bound(self):
        html = table(["随意列", "备注"], ["x", "y"])
        table_model, = doc_for(html).tables()
        self.assertEqual([], bind_table(table_model, "dispatch_daily"))

    def test_field_archive_covers_nine_material_groups(self):
        self.assertEqual(9, len(GROUP_FIELDS))
        self.assertIn("max_personnel", GROUP_FIELDS["design"])
        self.assertIn("acceptance_passed", GROUP_FIELDS["relief_acceptance"])


class FactGenerationTests(unittest.TestCase):
    def test_dispatch_table_becomes_an_action_fact_with_evidence(self):
        html = table(["工作面", "动作", "状态", "执行人角色", "时间"],
                     ["W1", "停工", "已完成", "调度室", "2026-10-01T08:00:00"])
        facts = facts_from_document(doc_for(html), "dispatch_daily")
        self.assertEqual(1, len(facts))
        fact = facts[0]
        self.assertEqual("action", fact["kind"])
        self.assertEqual("W1", fact["workface"])
        self.assertEqual("2026-10-01T08:00:00", fact["time"])
        self.assertEqual("actual_record", fact["source_role"])
        self.assertEqual("停工", fact["fields"]["action"])
        self.assertTrue(fact["evidence_refs"])
        again = facts_from_document(doc_for(html), "dispatch_daily")[0]
        self.assertEqual(fact["evidence_refs"], again["evidence_refs"])

    def test_threshold_table_becomes_a_requirement_fact(self):
        html = table(["方法", "指标", "测点", "实测值", "单位", "批复", "有效起始", "适用范围"],
                     ["应力监测法", "煤体应力", "P1", "10", "MPa", "已批复", "2026-09-23", "W1"])
        fact, = facts_from_document(doc_for(html), "threshold")
        self.assertEqual("threshold", fact["kind"])
        self.assertEqual("requirement", fact["source_role"])
        self.assertEqual("已批复", fact["fields"]["approved"])
        self.assertEqual("pressure", fact["quantities"]["value"]["dimension"])

    def test_rules_accept_canonical_facts(self):
        dispatch = table(["工作面", "动作", "状态", "时间"],
                         ["W1", "停工", "已完成", "2026-10-01T08:00:00"])
        facts = facts_from_document(doc_for(dispatch), "dispatch_daily")
        run = {"run_id": "r1", "spec": {"source_system": "local", "mine_code": "M1",
                                        "environment": "local", "item_code": "stop_work_unload",
                                        "check_cycle": "2026-10",
                                        "definition_version": "statutory-local-v1"}}
        evidence = []
        for fact in facts:
            for ref in fact["evidence_refs"]:
                evidence.append({"run_id": "r1", "source_system": "local", "mine_code": "M1",
                                 "environment": "local", "snapshot_id": "s1", "evidence_id": ref,
                                 "content_hash": "hash-1", "text": "cell", "locator": {"page": 1},
                                 "source_role": fact["source_role"]})
        snapshot = {"run_id": "r1", "source_system": "local", "mine_code": "M1",
                    "environment": "local", "snapshot_id": "s1", "profile": "local-documents",
                    "files": [{"file_id": "f1", "content_hash": "hash-1"}],
                    "evidence": evidence, "facts": facts, "source_complete": True,
                    "coverage_gaps": [],
                    "observation": {"start": "2026-10-01T00:00:00", "end": "2026-10-01T23:59:59"}}
        result = evaluate_documents(run, snapshot)
        self.assertEqual("pending", result["status"])
        # 事实被规则接受（未抛错），但因为缺少触发事件与完整链条，只能收束为待补证
        self.assertTrue(result["coverage_gaps"])
        self.assertFalse(result["findings"])


if __name__ == "__main__":
    unittest.main()
