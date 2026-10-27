"""P3 layer: content-addressed cache, review-as-labelling and gap diagnostics."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compliance_service.cache import NormalisationCache, cache_key
from compliance_service.diagnostics import priorities, reason_summary
from compliance_service.review import (accuracy_against_gold, apply_review, gold_of,
                                       summarise)


class CacheTests(unittest.TestCase):
    def test_key_depends_on_bytes_and_versions_only(self):
        base = cache_key("a" * 16, contract_version="c1", pipeline_version="p1")
        self.assertEqual(base, cache_key("a" * 16, contract_version="c1", pipeline_version="p1"))
        self.assertNotEqual(base, cache_key("a" * 16, contract_version="c2", pipeline_version="p1"))
        self.assertNotEqual(base, cache_key("a" * 16, contract_version="c1", pipeline_version="p2"))
        with self.assertRaises(ValueError):
            cache_key("short", contract_version="c1", pipeline_version="p1")

    def test_roundtrip_and_second_run_avoids_recompute(self):
        with TemporaryDirectory() as directory:
            cache = NormalisationCache(directory, contract_version="c1", pipeline_version="p1")
            calls = []
            def compute():
                calls.append(1)
                return {"pages": 3, "receipts": [{"page": 1}]}
            first = cache.get_or_compute("deadbeefdeadbeef", compute)
            second = cache.get_or_compute("deadbeefdeadbeef", compute)
            self.assertEqual(first, second)
            self.assertEqual(1, len(calls))
            self.assertEqual({"hits": 1, "misses": 1}, cache.stats())

    def test_corrupted_entry_is_ignored_and_recomputed(self):
        with TemporaryDirectory() as directory:
            cache = NormalisationCache(directory, contract_version="c1", pipeline_version="p1")
            path = cache.save("deadbeefdeadbeef", {"v": 1})
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(cache.load("deadbeefdeadbeef"))
            self.assertEqual({"v": 2}, cache.get_or_compute("deadbeefdeadbeef", lambda: {"v": 2}))


class ReviewTests(unittest.TestCase):
    def test_three_actions_produce_gold_without_touching_the_run(self):
        confirm = apply_review("measurement.value", "8", "confirm", reviewer="zhang")
        corrected = apply_review("measurement.value", "8", "correct", corrected="8.2")
        rejected = apply_review("measurement.value", "8", "reject", reason="UNREADABLE")
        self.assertEqual({"field_key": "measurement.value", "value": "8", "verdict": "correct"},
                         gold_of(confirm))
        self.assertEqual("8.2", gold_of(corrected)["value"])
        self.assertEqual("rejected", gold_of(rejected)["verdict"])
        summary = summarise([confirm, corrected, rejected])
        self.assertEqual({"confirm": 1, "correct": 1, "reject": 1}, summary["counts"])
        self.assertEqual(0.3333, summary["rejection_rate"])
        self.assertEqual(0.3333, accuracy_against_gold([confirm, corrected, rejected])["precision"])

    def test_invalid_reviews_are_rejected(self):
        with self.assertRaises(ValueError):
            apply_review("f", "v", "approve")
        with self.assertRaises(ValueError):
            apply_review("f", "v", "correct")
        with self.assertRaises(ValueError):
            apply_review("f", "v", "reject", reason="because")


class DiagnosticsTests(unittest.TestCase):
    def test_gap_reasons_are_bucketed_and_prioritised(self):
        snapshot = {"page_receipts": [
            {"file_id": "f1", "status": "partially_extracted", "reason_codes": ["VL_UNDERREAD", "LOW_DPI"]},
            {"file_id": "f1", "status": "rejected_by_quality", "reason_codes": ["VL_UNDERREAD"]},
            {"file_id": "f2", "status": "extracted", "reason_codes": []},
        ]}
        summary = reason_summary(snapshot)
        self.assertEqual(3, summary["pages"])
        self.assertEqual(1, summary["blocked_pages"])
        self.assertEqual({"识别不确定": 2, "采集质量": 1}, summary["buckets"])
        self.assertEqual(["识别不确定", "采集质量"], priorities(summary))
        self.assertEqual(2, summary["per_file"]["f1"]["VL_UNDERREAD"])

    def test_empty_snapshot_is_safe(self):
        summary = reason_summary({})
        self.assertEqual({"pages": 0, "blocked_pages": 0, "blocked_rate": 0.0,
                          "buckets": {}, "per_file": {}}, summary)
        self.assertEqual([], priorities(summary))


class LayoutReviewTests(unittest.TestCase):
    def test_confirm_registers_a_variant_and_reject_does_not(self):
        from compliance_service.canonical import document_from_blocks
        from compliance_service.review import layout_review
        from compliance_service.variant_store import VariantStore

        table, = document_from_blocks([{"label": "table", "content":
            "<table><tr><td>等级</td><td>预警指标</td><td>采取措施</td></tr>"
            "<tr><td>a</td><td>dev<25%</td><td>正常生产</td></tr></table>"}],
            file_id="f1", page=1).tables()
        confirmed = layout_review(table, "threshold", "formula_table", mine_code="WJP",
                                 reviewer="zhang")
        self.assertEqual("confirm", confirmed["record"]["action"])
        with TemporaryDirectory() as directory:
            store = VariantStore(Path(directory) / "variants.json")
            self.assertTrue(store.add(confirmed["variant"])["added"])
            self.assertEqual(1, len(store.load()))
        rejected = layout_review(table, "threshold", "formula_table", action="reject")
        self.assertEqual("reject", rejected["record"]["action"])


if __name__ == "__main__":
    unittest.main()
