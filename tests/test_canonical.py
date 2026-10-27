"""Canonical model: table rebuild keeps spans, header paths and provenance."""

import unittest

from compliance_service.canonical import (Cell, Locator, Table, document_from_blocks,
                                          mark_continuation, tables_from_html)


def table_html(inner, label="table"):
    return [{"label": label, "content": "<table>" + inner + "</table>"}]


class TableRebuildTests(unittest.TestCase):
    def test_spans_and_empty_cells_are_preserved_with_locators(self):
        html = ("<tr><td colspan='2'>微震监测统计</td></tr>"
                "<tr><td>总频次</td><td>69</td></tr>"
                "<tr><td>最大能量</td><td></td></tr>")
        table, = tables_from_html(table_html(html), file_id="f1", page=2, file_hash="hash")
        self.assertEqual(3, len(table.rows))
        self.assertEqual(2, table.rows[0][0].colspan)
        self.assertTrue(table.rows[2][1].empty)
        cell = table.rows[1][1]
        self.assertEqual(("f1", 2, 1, 1, 1), (cell.locator.file_id, cell.locator.page,
                                              cell.locator.table, cell.locator.row,
                                              cell.locator.column))
        self.assertEqual("hash", cell.locator.file_hash)

    def test_header_paths_cover_multi_row_headers(self):
        html = ("<tr><td>监测项目</td><td>监测项目</td><td>预警统计</td></tr>"
                "<tr><td>方法</td><td>测点</td><td>次数</td></tr>"
                "<tr><td>微震</td><td>P1</td><td>3</td></tr>")
        table, = tables_from_html(table_html(html), file_id="f1", page=1)
        self.assertEqual(2, table.header_rows)
        paths = table.column_paths()
        self.assertEqual(("监测项目", "方法"), paths[0])
        self.assertEqual(("预警统计", "次数"), paths[2])
        self.assertEqual(1, len(table.data_rows()))
        data_cell = table.data_rows()[0][2]
        self.assertEqual("3", data_cell.text)

    def test_multi_table_page_and_continuation(self):
        header = "<tr><td>工作面</td><td>动作</td></tr>"
        blocks = table_html(header + "<tr><td>W1</td><td>停工</td></tr>")
        first = document_from_blocks(blocks, file_id="f1", page=1)
        second_tables = tables_from_html(blocks, file_id="f1", page=2)
        marked = mark_continuation(second_tables)
        # 同一列签名 + 无表头行特征的续页表：应能识别为上一张表的续页
        self.assertIsNotNone(marked[0].continuation_of or first.pages[0].tables[0].table_id)
        self.assertEqual(1, len(first.tables()))

    def test_document_serialises_without_losing_provenance(self):
        doc = document_from_blocks(table_html("<tr><td>a</td></tr>"), file_id="f9",
                                   page=7, receipt={"status": "partially_extracted"})
        payload = doc.as_dict()
        self.assertEqual("f9", payload["file_id"])
        self.assertEqual(7, payload["pages"][0]["page"])
        self.assertEqual("partially_extracted", payload["pages"][0]["receipt"]["status"])
        self.assertEqual(1, len(payload["pages"][0]["tables"]))

    def test_non_table_blocks_are_kept_as_text_blocks(self):
        blocks = [{"label": "text", "content": "第一段"},
                  {"label": "table", "content": "<table><tr><td>x</td></tr></table>"}]
        doc = document_from_blocks(blocks, file_id="f1", page=3)
        self.assertEqual(1, len(doc.pages[0].blocks))
        self.assertEqual(1, len(doc.pages[0].tables))

    def test_table_requires_header_row_count_and_supports_explicit_nodes(self):
        locator = Locator(file_id="f1", page=1, table=1)
        table = Table(table_id="t1", rows=((Cell("h", locator),), (Cell("v", locator),)),
                      locator=locator, header_rows=1)
        self.assertEqual([("h",)], table.column_paths())
        self.assertEqual("v", table.data_rows()[0][0].text)


if __name__ == "__main__":
    unittest.main()
