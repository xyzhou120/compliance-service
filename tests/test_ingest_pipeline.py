"""Page pipeline: static receipts, channel planning, VL underread and number gate."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document

from compliance_service.contracts import RunSpec
from compliance_service.ingest import (build_page_receipts, enabled, page_trace, plan_channels,
                                       read_page, verified_numbers)
from compliance_service.sources import LocalDocumentSource


class FakeChannels:
    def __init__(self, layout=None, vl=None, no_layout=None, ocr_lines=None):
        self._layout = layout or []
        self._vl = vl or []
        self._no_layout = no_layout or []
        self._ocr = ocr_lines or []
        self.calls = []

    def layout(self, image, **_kwargs):
        self.calls.append("layout")
        return self._layout

    def vl_ocr(self, image, **_kwargs):
        self.calls.append("vl_ocr")
        return self._vl

    def vl_ocr_no_layout(self, image, **_kwargs):
        self.calls.append("vl_ocr_no_layout")
        return self._no_layout

    def ocr(self, image, **_kwargs):
        self.calls.append("ocr")
        return {"rec_texts": self._ocr}


def table_block(rows=3):
    cells = "".join("<tr><td>%d</td><td>值</td></tr>" % i for i in range(rows))
    return {"label": "table", "content": "<table>" + cells + "</table>"}


class StaticReceiptTests(unittest.TestCase):
    def test_parsed_pages_become_receipts_with_reasons(self):
        parsed = {"blocks": [{"text": "abcde", "locator": {"page": 1}}],
                  "coverage": [{"unit_id": "page:1", "state": "readable"},
                               {"unit_id": "page:2", "state": "unreadable",
                                "reason": "page_image_requires_ocr"}]}
        receipts = build_page_receipts([({"file_id": "f1"}, parsed)])
        self.assertEqual([1, 2], [r["page"] for r in receipts])
        self.assertEqual("native_text", receipts[0]["page_type"])
        self.assertEqual("extracted", receipts[0]["status"])
        self.assertEqual("raster_scan", receipts[1]["page_type"])
        self.assertEqual("partially_extracted", receipts[1]["status"])
        self.assertEqual("f1", receipts[1]["file_id"])

    def test_receipts_are_opt_in_and_attach_to_the_snapshot(self):
        self.assertFalse(enabled({}))
        self.assertTrue(enabled({"COMPLIANCE_PAGE_RECEIPTS": "1"}))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            document = Document()
            table = document.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "工作面"
            table.rows[0].cells[1].text = "动作"
            table.rows[1].cells[0].text = "W1"
            table.rows[1].cells[1].text = "停工"
            document.save(root / "doc.docx")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"source_system": "t", "mine_code": "M", "environment": "test",
                                            "period": {"start": "2026-10-01", "end": "2026-10-31"},
                                            "source_complete": True,
                                            "files": [{"file_id": "f", "path": "doc.docx",
                                                       "role": "actual_record"}]}), encoding="utf-8")
            spec = RunSpec("t", "M", "stop_work_unload", "2026-10", {"manifest": str(manifest)},
                           environment="test", callback_mode="none")
            source = LocalDocumentSource(root / "cache")
            off = source({"run_id": "r1", "spec": spec.to_dict()})
            with patch.dict(os.environ, {"COMPLIANCE_PAGE_RECEIPTS": "1"}):
                on = source({"run_id": "r2", "spec": spec.to_dict()})
        self.assertEqual([], off["page_receipts"])
        self.assertTrue(on["page_receipts"])
        self.assertEqual("f", on["page_receipts"][0]["file_id"])
        trace = page_trace(on)
        self.assertIn("第", trace[0]["summary"])


class NumberGateTests(unittest.TestCase):
    def test_only_agreed_numbers_are_verified(self):
        result = verified_numbers("总频次 69 最大能量 4.92x10^3", "总频次 69 最大能量 4.92x10^3 备注 7")
        self.assertIn("69", result["verified"])
        self.assertIn("4.92e3", result["verified"])
        self.assertEqual(["7"], result["only_ocr"])
        self.assertEqual([], result["verified"][1:] if False else result["only_vl"])

    def test_lost_exponent_in_classic_ocr_is_reported(self):
        result = verified_numbers("能量 4.92x10^3", "能量 4.92x10J")
        self.assertEqual(1, result["exponent_lost"]["ocr"])
        self.assertNotIn("4.92e3", result["verified"])


class ReadPageTests(unittest.TestCase):
    def test_healthy_page_needs_no_underread_retry(self):
        channels = FakeChannels(layout=[{"label": "table"}, {"label": "text"}],
                                vl=[{"label": "table", "content": "<table><tr><td>总频次</td><td>69</td></tr>"
                                                                 "<tr><td>最大能量</td><td>4.92x10^3</td></tr></table>"}],
                                ocr_lines=["总频次 69", "最大能量 4.92x10^3"])
        outcome = read_page(channels, b"img", page=1)
        self.assertNotIn("VL_UNDERREAD", outcome["receipt"]["reason_codes"])
        self.assertEqual(["layout", "vl_ocr", "ocr"], channels.calls)
        self.assertIn("69", outcome["numbers"]["verified"])

    def test_underread_page_retries_without_layout_and_degrades(self):
        # 实测形状：布局 9 个图区 + 1 表格，VL 几乎没字，经典 OCR 266 行
        channels = FakeChannels(layout=[{"label": "table"}] + [{"label": "chart"}] * 9,
                                vl=[{"label": "table", "content": ""}],
                                no_layout=[{"label": "text", "content": ""}],
                                ocr_lines=["行%d 12MPa" % i for i in range(266)])
        outcome = read_page(channels, b"img", page=4)
        self.assertEqual(["layout", "vl_ocr", "ocr", "vl_ocr_no_layout"], channels.calls)
        self.assertIn("VL_UNDERREAD", outcome["receipt"]["reason_codes"])
        self.assertEqual("partially_extracted", outcome["receipt"]["status"])

    def test_layout_free_retry_can_recover_the_page(self):
        recovered = "\n".join("行%d 12MPa" % i for i in range(60))
        channels = FakeChannels(layout=[{"label": "table"}] + [{"label": "chart"}] * 9,
                                vl=[],
                                no_layout=[{"label": "text", "content": recovered}],
                                ocr_lines=["行%d 12MPa" % i for i in range(60)])
        outcome = read_page(channels, b"img", page=5)
        self.assertNotIn("VL_UNDERREAD", outcome["receipt"]["reason_codes"])
        self.assertIn("vl_ocr_no_layout", [a["channel"] for a in outcome["attempts"]])

    def test_page_budget_degrades_instead_of_stalling(self):
        # 通道很慢时，页预算必须让这一页降级返回，而不是整批卡死
        calls = {"layout": 0, "vl": 0, "ocr": 0}

        class SlowChannels(FakeChannels):
            def layout(self, image, **_kwargs):
                calls["layout"] += 1
                return [{"label": "table"}]

            def vl_ocr(self, image, **_kwargs):
                calls["vl"] += 1
                return [{"label": "table", "content": "<table><tr><td>x</td></tr></table>"}]

            def ocr(self, image, **_kwargs):
                calls["ocr"] += 1
                return {"rec_texts": []}

        outcome = read_page(SlowChannels(layout=[], vl=[], ocr_lines=[]), b"img", page=1,
                            budget_seconds=-1)
        self.assertIn("PAGE_BUDGET_EXCEEDED", outcome["receipt"]["reason_codes"])
        self.assertEqual("partially_extracted", outcome["receipt"]["status"])
        self.assertEqual(0, calls["ocr"])          # 超预算后不再调用后续通道
        self.assertEqual([], outcome["numbers"]["verified"])

    def test_channel_timeouts_are_bounded(self):
        seen = {}

        class RecordingChannels(FakeChannels):
            def layout(self, image, **kwargs):
                seen["layout"] = kwargs.get("timeout")
                return [{"label": "table"}]

            def vl_ocr(self, image, **kwargs):
                seen["vl"] = kwargs.get("timeout")
                return []

            def ocr(self, image, **kwargs):
                seen["ocr"] = kwargs.get("timeout")
                return {"rec_texts": ["行"] * 30}

        read_page(RecordingChannels(layout=[], vl=[], ocr_lines=[]), b"img", page=1)
        self.assertLessEqual(seen["layout"], 30)
        self.assertLessEqual(seen["vl"], 60)
        self.assertLessEqual(seen["ocr"], 45)

    def test_channel_plan_matches_page_types(self):
        self.assertEqual(("layout", "vl_ocr", "ocr"), plan_channels("raster_scan"))
        self.assertEqual((), plan_channels("unknown"))
        self.assertEqual(("native",), plan_channels("native_text"))


if __name__ == "__main__":
    unittest.main()
