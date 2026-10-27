"""Canonical facts bridge: table rows become rule-engine facts with evidence."""

import unittest

from compliance_service.canonical_facts import facts_for_group, merge_into_snapshot


def evidence_row(file_id, row, headers, cells, evidence_id):
    return {"evidence_id": evidence_id, "file_id": file_id, "text": " ".join(cells),
            "locator": {"row": row, "page": 1},
            "metadata": {"cells": cells, "headers": headers}, "source_role": "actual_record"}


HEADERS = ["工作面", "动作", "状态", "执行人角色", "时间"]


class BridgeTests(unittest.TestCase):
    def test_table_rows_become_action_facts_with_evidence(self):
        evidence = [evidence_row("f1", 4, HEADERS,
                                 ["W1", "停工", "已完成", "调度室", "2026-10-01T08:00:00"], "ev-1")]
        facts = facts_for_group(evidence, "dispatch_daily")
        self.assertEqual(1, len(facts))
        fact = facts[0]
        self.assertEqual("action", fact["kind"])
        self.assertEqual("W1", fact["workface"])
        self.assertEqual("停工", fact["fields"]["action"])
        self.assertEqual(["ev-1"], fact["evidence_refs"])
        self.assertEqual("actual_record", fact["source_role"])

    def test_requirement_groups_mark_requirement_role(self):
        headers = ["方法", "指标", "测点", "实测值", "单位", "批复", "有效起始"]
        evidence = [evidence_row("f2", 2, headers,
                                 ["应力监测法", "煤体应力", "P1", "10", "MPa", "已批复", "2026-09-23"],
                                 "ev-2")]
        fact, = facts_for_group(evidence, "threshold")
        self.assertEqual("threshold", fact["kind"])
        self.assertEqual("requirement", fact["source_role"])
        self.assertEqual("已批复", fact["fields"]["approved"])

    def test_rows_without_group_fields_are_skipped(self):
        evidence = [evidence_row("f3", 1, ["序号", "备注"], ["1", "无"], "ev-3")]
        self.assertEqual([], facts_for_group(evidence, "dispatch_daily"))

    def test_merge_appends_without_touching_legacy_facts(self):
        evidence = [evidence_row("f1", 4, HEADERS,
                                 ["W1", "停工", "已完成", "调度室", "2026-10-01T08:00:00"], "ev-1")]
        snapshot = {"files": [{"file_id": "f1", "material_group": "dispatch_daily"}],
                    "evidence": evidence,
                    "facts": [{"fact_id": "legacy-1", "kind": "action", "fields": {},
                               "evidence_refs": ["ev-1"]}]}
        merged = merge_into_snapshot(snapshot)
        self.assertEqual(2, len(merged["facts"]))
        self.assertEqual("legacy-1", merged["facts"][0]["fact_id"])
        self.assertEqual(1, merged["canonical_fact_count"])
        self.assertEqual([], merge_into_snapshot({"files": [], "evidence": [], "facts": []})["facts"])


if __name__ == "__main__":
    unittest.main()
