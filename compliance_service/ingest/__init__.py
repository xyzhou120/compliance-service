"""Normalisation layer: page routing, receipts and bounded recognition channels."""

from .channels import (LAYOUT_PATH, OCR_PATH, TABLE_CELL_PATH, VL_OCR_NO_LAYOUT_PATH, VL_OCR_PATH,
                       ChannelUnavailable, RecognitionChannels, multipart_body)
from .pipeline import (ENV_FLAG, build_page_receipts, enabled, page_trace, plan_channels,
                       read_page, verified_numbers)
from .receipt import (PAGE_TYPES, QUALITY_TIERS, STATUSES, PageReceipt, is_vl_underread,
                      number_agreement, normalized_numbers)
from .router import build_receipt, classify_page, quality_tier, render_dpi_for

__all__ = [
    "ChannelUnavailable", "RecognitionChannels", "multipart_body",
    "LAYOUT_PATH", "OCR_PATH", "VL_OCR_PATH", "VL_OCR_NO_LAYOUT_PATH", "TABLE_CELL_PATH",
    "PAGE_TYPES", "QUALITY_TIERS", "STATUSES", "PageReceipt", "is_vl_underread",
    "number_agreement", "normalized_numbers",
    "build_receipt", "classify_page", "quality_tier", "render_dpi_for",
    "ENV_FLAG", "build_page_receipts", "enabled", "page_trace", "plan_channels", "read_page",
    "verified_numbers",
]
