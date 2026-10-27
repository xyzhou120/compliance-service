"""Variant registry: review-confirmed layouts persist and get reused."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compliance_service.canonical import document_from_blocks
from compliance_service.variant_store import VariantStore, discover, variant_payload
from compliance_service.variants import register_variant


def make_tables(rows_list):
    blocks = []
    for rows in rows_list:
        html = "<table>" + "".join("<tr>" + "".join("<td>%s</td>" % c for c in row) + "</tr>"
                                    for row in rows) + "</table>"
        blocks.append({"label": "table", "content": html})
    return document_from_blocks(blocks, file_id="f1", page=1).tables()


class StoreTests(unittest.TestCase):
    def test_add_and_reload_keeps_signature_and_reader(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "variants.json"
            store = VariantStore(path)
            table, = make_tables([[["等级", "预警指标", "采取措施"], ["a", "x", "y"]]])
            variant = register_variant(table, "threshold", "formula_table", mine_code="WJP")
            result = store.add(variant)
            self.assertTrue(result["added"])
            reloaded = VariantStore(path).load()
            self.assertEqual(1, len(reloaded))
            self.assertEqual(("等级", "预警指标", "采取措施"), reloaded[0].header_signature)
            self.assertEqual("formula_table", reloaded[0].reader)
            self.assertIn(variant.variant_id, [item["variant_id"] for item in
                                               [variant_payload(v) for v in reloaded]])

    def test_duplicate_add_is_idempotent_but_conflicting_reader_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = VariantStore(Path(directory) / "v.json")
            table, = make_tables([[["等级", "预警指标", "采取措施"], ["a", "x", "y"]]])
            variant = register_variant(table, "threshold", "formula_table", mine_code="WJP")
            store.add(variant)
            self.assertFalse(store.add(variant)["added"])
            other = register_variant(table, "threshold", "field_columns", mine_code="WJP")
            with self.assertRaises(ValueError):
                store.add(other)

    def test_shipped_variants_are_always_available(self):
        with TemporaryDirectory() as directory:
            store = VariantStore(Path(directory) / "v.json")
            self.assertGreaterEqual(len(store.all()), 3)

    def test_discover_lists_only_uncovered_layouts(self):
        known_table, = make_tables([[["应力在线预警模型", "掘进工作面", "蓝", "黄", "橙", "红"],
                                     ["浅基点", "/", "≥12", "≥14", "≥16"]]])
        new_table, = make_tables([[["工作面", "班次", "进尺", "备注"], ["101", "夜班", "4", ""]]])
        pending = discover([known_table, new_table], "threshold")
        self.assertEqual(1, len(pending))
        self.assertEqual(["工作面", "班次", "进尺", "备注"], pending[0]["signature"])
        self.assertEqual(1, pending[0]["tables"])
        self.assertFalse(pending[0]["suspicious_header"])
        data_row, = make_tables([[["10", "综掘", "42盘区", "4208运顺"], ["a", "b", "c", "d"]]])
        flagged = discover([data_row], "threshold")
        self.assertTrue(flagged[0]["suspicious_header"])


if __name__ == "__main__":
    unittest.main()
