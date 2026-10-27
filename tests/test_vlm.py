"""Vision table transcription: gated, bounded and never guessed."""

import json
import unittest

from compliance_service.vlm import (VisionChannel, VisionUnavailable, extract_html,
                                    validate_table)


HTML = ("<table><tr><td>方法</td><td>指标</td><td>测点</td><td>实测值</td><td>单位</td></tr>"
        "<tr><td>应力监测法</td><td>煤体应力</td><td>P1</td><td>10</td><td>MPa</td></tr>"
        "<tr><td>钻屑法</td><td>钻粉率指数</td><td>P2</td><td>1.2</td><td>1</td></tr></table>")


def transport_returning(content):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append(json.loads(body.decode("utf-8")))
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    transport.calls = calls
    return transport


class TableGateTests(unittest.TestCase):
    def test_well_formed_table_passes(self):
        table, gate = VisionChannel(transport=transport_returning(HTML), api_key_env="PATH").transcribe_table(
            b"img", file_id="f1", page=3)
        self.assertIsNotNone(table)
        self.assertEqual("table_verified", gate["status"])
        self.assertEqual(2, gate["data_rows"])
        self.assertEqual(("方法",), table.column_paths()[0])
        self.assertEqual("10", table.data_rows()[0][3].text)

    def test_missing_header_or_sparse_cells_are_rejected(self):
        single_cell = "<table><tr><td>微震监测统计</td></tr></table>"
        table, gate = VisionChannel(transport=transport_returning(single_cell), api_key_env="PATH").transcribe_table(
            b"img", file_id="f1", page=1)
        self.assertIsNone(table)
        self.assertIn(gate["status"], {"table_rejected", "table_unverified"})
        no_table = VisionChannel(transport=transport_returning("抱歉我无法识别"), api_key_env="PATH").transcribe_table(
            b"img", file_id="f1", page=1)
        self.assertEqual("NO_HTML", no_table[1]["reasons"][0])

    def test_spans_are_reported_not_hidden(self):
        spans = ("<table><tr><td colspan='2'>监测项目</td><td>值</td></tr>"
                 "<tr><td>a</td><td>b</td><td>1</td></tr></table>")
        table, gate = VisionChannel(transport=transport_returning(spans), api_key_env="PATH").transcribe_table(
            b"img", file_id="f1", page=2)
        self.assertIn("HAS_SPANS", gate["warnings"])
        self.assertEqual("table_verified", gate["status"])

    def test_prompt_forbids_guessing_and_requires_exponents(self):
        transport = transport_returning(HTML)
        VisionChannel(transport=transport, api_key_env="PATH").transcribe_table(b"img", file_id="f1", page=1)
        prompt = transport.calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("<table>", prompt)
        self.assertIn("10^4", prompt)
        self.assertIn("不要猜", prompt)

    def test_failures_are_bounded_and_reported(self):
        def broken(url, headers, body, timeout):
            raise TimeoutError("slow")
        channel = VisionChannel(transport=broken, api_key_env="PATH", retries=1)
        with self.assertRaises(VisionUnavailable):
            channel.complete(b"img", "x")
        self.assertEqual({"calls": 2, "failures": 2}, channel.stats())

    def test_missing_key_fails_closed(self):
        channel = VisionChannel(transport=transport_returning(HTML), api_key_env="NO_SUCH_KEY")
        channel.api_key = ""
        with self.assertRaises(VisionUnavailable):
            channel.complete(b"img", "x")

    def test_extract_html_tolerates_leading_prose(self):
        self.assertEqual("<table><tr><td>a</td></tr></table>",
                         extract_html("这是表格：<TABLE><tr><td>a</td></tr></table> 完成").lower())


if __name__ == "__main__":
    unittest.main()
