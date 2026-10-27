"""Vision fallback inside the page pipeline (degenerate VL tables)."""

import unittest

from compliance_service.canonical import Table, Cell, Locator
from compliance_service.ingest import read_page
from compliance_service.vlm import validate_table


class FakeChannels:
    def __init__(self, layout, vl, ocr_lines):
        self._layout, self._vl, self._ocr = layout, vl, ocr_lines
        self.calls = []

    def layout(self, image, **_kwargs):
        self.calls.append("layout")
        return self._layout

    def vl_ocr(self, image, **_kwargs):
        self.calls.append("vl_ocr")
        return self._vl

    def vl_ocr_no_layout(self, image, **_kwargs):
        self.calls.append("vl_ocr_no_layout")
        return [{"label": "ocr", "content": "\n".join(self._ocr)}]

    def ocr(self, image, **_kwargs):
        self.calls.append("ocr")
        return {"rec_texts": self._ocr}


class FakeVision:
    def __init__(self, table=None, status="table_verified"):
        self.table = table
        self.status = status
        self.calls = []

    def transcribe_table(self, image, *, file_id, page, file_hash=None):
        self.calls.append((file_id, page))
        if self.table is None:
            return None, {"status": self.status, "reasons": ["SPARSE_CELLS"]}
        return self.table, {"status": self.status, "reasons": []}


def simple_table():
    locator = Locator(file_id="f1", page=2, table=1)
    rows = ((Cell("方法", locator), Cell("实测值", locator), Cell("单位", locator)),
            (Cell("应力监测法", locator), Cell("10", locator), Cell("MPa", locator)))
    return Table(table_id="p2-t1", rows=rows, locator=locator, header_rows=1)


class VisionFallbackTests(unittest.TestCase):
    def test_degenerate_table_triggers_vision_and_is_marked(self):
        channels = FakeChannels(layout=[{"label": "table"}],
                                vl=[{"label": "table", "content": "<table><tr><td>微震监测统计</td></tr></table>"}],
                                ocr_lines=["行"] * 30)
        vision = FakeVision(simple_table())
        outcome = read_page(channels, b"img", page=2, vision=vision, file_id="f1")
        self.assertEqual([("f1", 2)], vision.calls)
        self.assertIn("TABLE_FROM_VISION", outcome["receipt"]["reason_codes"])
        tables = [b for b in outcome["vl_blocks"] if b.get("label") == "table"]
        self.assertEqual(1, len(tables))
        self.assertIn("应力监测法", tables[0]["content"])
        self.assertIn("vision_table", [a["channel"] for a in outcome["attempts"]])

    def test_rejected_vision_table_is_not_used(self):
        channels = FakeChannels(layout=[{"label": "table"}],
                                vl=[{"label": "table", "content": "<table><tr><td>x</td></tr></table>"}],
                                ocr_lines=["行"] * 30)
        outcome = read_page(channels, b"img", page=3, vision=FakeVision(None), file_id="f1")
        self.assertIn("VISION_TABLE_UNVERIFIED", outcome["receipt"]["reason_codes"])
        self.assertFalse(any(b.get("label") == "table" and "应力" in str(b.get("content"))
                             for b in outcome["vl_blocks"]))

    def test_each_table_region_is_tried_before_giving_up(self):
        # 整页混排多表时，逐个区域裁块尝试，而不是只送第一块
        from compliance_service.vlm import validate_table
        calls = []

        class RegionVision:
            def __init__(self):
                self.calls = 0

            def transcribe_table(self, image, *, file_id, page, file_hash=None):
                self.calls += 1
                calls.append(image)
                if self.calls < 2:
                    return None, {"status": "table_rejected", "reasons": ["NO_HEADER"]}
                return simple_table(), {"status": "table_verified", "reasons": []}

        channels = FakeChannels(layout=[{"label": "table", "bbox": [0, 0, 100, 100]},
                                        {"label": "table", "bbox": [0, 100, 100, 200]}],
                                vl=[{"label": "table", "content": "<table><tr><td>x</td></tr></table>"}],
                                ocr_lines=["行"] * 30)
        vision = RegionVision()
        outcome = read_page(channels, b"img", page=1, vision=vision, file_id="f1",
                            cropper=lambda image, bbox: image + str(bbox).encode())
        self.assertEqual(2, vision.calls)
        self.assertIn("TABLE_FROM_VISION", outcome["receipt"]["reason_codes"])
        self.assertTrue(any(a.get("channel") == "vision_table_region" for a in outcome["attempts"]))

    def test_sparse_but_structured_table_passes_the_gate(self):
        from compliance_service.vlm import validate_table
        locator = Locator(file_id="f1", page=1, table=1)
        rows = ((Cell("工作面", locator), Cell("动作", locator), Cell("设备", locator),
                 Cell("备注", locator)),
                (Cell("W1", locator), Cell("停工", locator), Cell("", locator), Cell("", locator)),
                (Cell("W2", locator), Cell("", locator), Cell("", locator), Cell("", locator)))
        table = Table(table_id="t", rows=rows, locator=locator, header_rows=1)
        gate = validate_table(table)
        self.assertEqual("table_verified", gate["status"])
        self.assertLess(gate["filled_ratio"], 0.5)

    def test_healthy_table_does_not_call_vision(self):
        healthy = "<table><tr><td>方法</td><td>值</td></tr><tr><td>a</td><td>1</td></tr></table>"
        channels = FakeChannels(layout=[{"label": "table"}],
                                vl=[{"label": "table", "content": healthy}],
                                ocr_lines=["行"] * 30)
        vision = FakeVision(simple_table())
        read_page(channels, b"img", page=1, vision=vision, file_id="f1")
        self.assertEqual([], vision.calls)


class TableGateSanityTests(unittest.TestCase):
    def test_gate_rejects_single_column_tables(self):
        locator = Locator(file_id="f1", page=1, table=1)
        table = Table(table_id="t", rows=((Cell("只有一列", locator),), (Cell("值", locator),)),
                      locator=locator, header_rows=1)
        gate = validate_table(table)
        self.assertEqual("table_unverified", gate["status"])


if __name__ == "__main__":
    unittest.main()
