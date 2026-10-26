"""Draft workbook scope exercised through real cross-file binary documents."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compliance_service.contracts import InvalidRequest
from compliance_service.fact_extraction import extract_facts
from compliance_service.ledger import Ledger
from compliance_service.rulebook import DEFAULT_RULEBOOK_VERSION
from compliance_service.service import InspectionService
from tests.test_document_pipeline import HEADERS, effect_rows, policy_rows, save_docx, save_xlsx, stop_rows


class CrossFileDraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = Ledger(self.root / "state.sqlite3")
        self.service = InspectionService(self.ledger)

    def make_manifest(self, records, *, coarse=False, wrong_face=False):
        records = deepcopy(records)
        for record in records:
            record["事件编号"] = ""
            if coarse:
                record["时间"] = "2026-10-23"
        if wrong_face:
            records[-1]["工作面"] = "W2"
            records[-1]["目标范围"] = "W2"
        policy = policy_rows()
        for record in policy:
            record.update({"事件编号": "", "时间": "2026-01-01", "有效起始": "2026-09-23", "有效截止": "2026-12-31"})
        save_docx(self.root / "warning.docx", records[:1])
        save_xlsx(self.root / "dispatch-and-measurements.xlsx", records[1:])
        save_docx(self.root / "approved-design.docx", policy)
        manifest = {"source_system": "draft-local", "mine_code": "mine-a", "environment": "test",
                    "period": {"start": "2026-10-01", "end": "2026-10-31"}, "source_complete": True,
                    "files": [{"file_id": "warning", "path": "warning.docx", "role": "actual_record"},
                              {"file_id": "records", "path": "dispatch-and-measurements.xlsx", "role": "actual_record"},
                              {"file_id": "design", "path": "approved-design.docx", "role": "requirement"}]}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def check(self, records, item="relief_effects", **options):
        manifest = self.make_manifest(records, **options)
        accepted = self.service.submit({"sourceSystem": "draft-local", "mineCode": "mine-a", "environment": "test",
                                        "checkCycle": "2026-10", "ruleVersion": DEFAULT_RULEBOOK_VERSION,
                                        "scope": {"manifest": str(manifest)}, "callbackMode": "none",
                                        "items": [{"itemCode": item}]})["items"][0]
        result = self.service.run_once()
        stored = self.ledger.get(accepted["run_id"], "draft-local", "mine-a", "test")
        return result, stored

    def test_both_checks_can_join_files_without_common_original_event_id(self):
        for item, records in (("stop_work_unload", stop_rows()), ("relief_effects", effect_rows())):
            with self.subTest(item=item):
                result, stored = self.check(records, item)
                self.assertEqual("pending", result["status"], result)
                self.assertEqual("pending", result["implemented_condition_status"])
                self.assertFalse(result["rule_coverage"]["full_rule_check_complete"])
                self.assertEqual(DEFAULT_RULEBOOK_VERSION, result["rule_baseline"]["version"])
                self.assertTrue(result["events"][0]["event_id"].startswith("internal-event-"))
                self.assertTrue(result["association_links"])
                self.assertTrue(result["event_evidence_chains"])
                sources = [source for chain in result["event_evidence_chains"] for step in chain["steps"] for source in step["sources"]]
                self.assertTrue(all(source["quote"] and source["locator"] for source in sources))
                self.assertGreaterEqual(len({source["file_id"] for source in sources}), 2)
                self.assertEqual(result, stored["result"])
                self.assertTrue(all(f["event_id"] for f in stored["snapshot"]["facts"]))

    def test_known_effect_equality_problem_survives_cross_file_association(self):
        result, _ = self.check(effect_rows("10"))
        self.assertEqual("pending", result["status"], result)
        self.assertEqual("no_pass", result["implemented_condition_status"])
        self.assertTrue(result["candidate_findings"])

    def test_same_day_only_and_wrong_workface_cannot_complete_the_chain(self):
        for option in ({"coarse": True}, {"wrong_face": True}):
            result, stored = self.check(effect_rows(), **option)
            self.assertEqual("pending", result["status"], result)
            self.assertTrue(stored["snapshot"]["coverage_gaps"])

    def test_evidence_role_and_link_references_cannot_be_forged(self):
        _, stored = self.check(effect_rows())
        snapshot = deepcopy(stored["snapshot"])
        snapshot["facts"][0]["source_role"] = "statement"
        from compliance_service.contracts import assert_snapshot_owner
        with self.assertRaises(InvalidRequest):
            assert_snapshot_owner(snapshot, stored)
        snapshot = deepcopy(stored["snapshot"])
        snapshot["association_links"][0]["evidence_refs"] = ["another-run-evidence"]
        with self.assertRaises(InvalidRequest):
            assert_snapshot_owner(snapshot, stored)
        snapshot = deepcopy(stored["snapshot"])
        snapshot["rule_baseline"]["version"] = "different-rule-version"
        with self.assertRaises(InvalidRequest):
            assert_snapshot_owner(snapshot, stored)

    def test_missing_id_extraction_retains_fact_without_inventing_an_original_id(self):
        headers = ["工作面", "时间", "动作", "状态"]
        evidence = {"evidence_id": "raw", "kind": "table_row", "source_role": "actual_record",
                    "text": "W1 已停工", "metadata": {"headers": headers,
                    "cells": ["W1", "2026-10-23T08:01:00", "停工", "已完成"]}}
        default = extract_facts([evidence])
        self.assertEqual([], default["facts"])
        raw = extract_facts([evidence], allow_unbound=True)
        self.assertEqual(1, len(raw["facts"]))
        self.assertIsNone(raw["facts"][0]["event_id"])
        self.assertEqual([], raw["gaps"])

    def test_generic_equipment_or_work_does_not_prove_actual_mining(self):
        for action in ("继续工作", "普通设备运行", "检修"):
            evidence = {"evidence_id": "raw", "kind": "table_row", "source_role": "actual_record",
                        "text": action, "metadata": {"headers": ["事件编号", "工作面", "时间", "动作", "状态"],
                        "cells": ["E1", "W1", "2026-10-23T08:01:00", action, "已完成"]}}
            result = extract_facts([evidence])
            self.assertFalse(any(f["kind"] == "action" and f["fields"]["action"] == "continue_work" for f in result["facts"]))
            self.assertTrue(result["gaps"])
