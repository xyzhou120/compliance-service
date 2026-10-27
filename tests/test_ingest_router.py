"""Routing, receipts and numeric agreement for the normalisation layer."""

import unittest

from compliance_service.ingest import (build_receipt, classify_page, is_vl_underread,
                                       number_agreement, normalized_numbers, quality_tier,
                                       render_dpi_for)


class RouterTests(unittest.TestCase):
    def test_native_text_and_table_pages(self):
        self.assertEqual("native_text", classify_page(native_chars=900)[0])
        page_type, reasons = classify_page(native_chars=900, layout_regions={"table": 2})
        self.assertEqual("native_table", page_type)
        self.assertTrue(any("table_regions" in r for r in reasons))

    def test_chart_page_is_not_treated_as_scanned_text(self):
        # 布局引擎报图区、文本稀疏：以前会被判"扫描页"送 OCR，图上的数字就成了正文
        page_type, reasons = classify_page(native_chars=120, drawings=40,
                                           layout_regions={"chart": 18, "table": 4})
        self.assertEqual("vector_chart", page_type)
        self.assertTrue(any("chart_regions" in r for r in reasons))

    def test_scanned_and_pasted_pages(self):
        self.assertEqual("raster_scan",
                         classify_page(native_chars=0, painted_images=2, image_area_share=0.9)[0])
        self.assertEqual("pasted_image",
                         classify_page(native_chars=40, painted_images=1, image_area_share=0.2)[0])
        self.assertEqual("mixed",
                         classify_page(native_chars=800, painted_images=3, image_area_share=0.4)[0])

    def test_unknown_page_is_never_extracted(self):
        receipt = build_receipt(page=3, native_chars=0)
        self.assertEqual("unknown", receipt.page_type)
        self.assertEqual("unreadable", receipt.status)
        self.assertIn("NO_TEXT_AND_NO_PAINTED_IMAGE", receipt.reason_codes)
        self.assertEqual(3, receipt.as_dict()["page"])

    def test_quality_tiers_and_rendered_dpi(self):
        self.assertEqual("good", quality_tier(dpi=250, sharpness=420))
        self.assertEqual("fair", quality_tier(dpi=150, sharpness=200))
        self.assertEqual("poor", quality_tier(dpi=98, sharpness=64))
        self.assertEqual(120, render_dpi_for(1080, 1440, long_side=2400, maximum=120))
        self.assertEqual(72, render_dpi_for(4000, 5000, long_side=2000))

    def test_poor_quality_page_cannot_be_reported_as_extracted(self):
        receipt = build_receipt(page=1, native_chars=0, painted_images=2, image_area_share=0.9,
                                dpi=98, sharpness=64)
        self.assertEqual("rejected_by_quality", receipt.status)
        self.assertIn("QUALITY_TIER_POOR", receipt.reason_codes)

    def test_vl_underread_is_detected_from_measured_failure(self):
        # 实测：某扫描页 VL 66 字，同页经典 OCR 266 行，布局报 9 个图区
        self.assertTrue(is_vl_underread(vl_chars=66, ocr_lines=266, figure_titles=9, table_regions=1))
        self.assertFalse(is_vl_underread(vl_chars=1028, ocr_lines=112, table_regions=1))
        self.assertFalse(is_vl_underread(vl_chars=0, ocr_lines=5, table_regions=0))


class NumberAgreementTests(unittest.TestCase):
    def test_latex_and_caret_exponents_compare_equal(self):
        latex, latex_lost = normalized_numbers("$ 4.92 \\times 10^{3} $J")
        caret, caret_lost = normalized_numbers("4.92×10^3J")
        self.assertEqual({"4.92e3"}, latex)
        self.assertEqual(latex, caret)
        self.assertEqual(0, latex_lost + caret_lost)

    def test_lost_exponent_is_flagged_and_never_compared(self):
        tokens, lost = normalized_numbers("最大能量 4.92×10J")
        self.assertEqual(1, lost)
        self.assertNotIn("4.92e1", tokens)

    def test_agreement_reports_common_and_per_channel_tokens(self):
        result = number_agreement({"1", "2", "3"}, {"2", "3", "4"}, {"3", "4", "5"})
        self.assertEqual(["3"], result["common"])
        self.assertEqual([["1", "2"], ["2", "4"], ["4", "5"]], result["per_channel_only"])


if __name__ == "__main__":
    unittest.main()
