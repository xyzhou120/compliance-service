"""Page-level receipts for the normalisation layer.

A receipt is the only thing downstream code may rely on when deciding whether a
page can support a business verdict. It records what the page is, how good the
input was, which channels ran, and why anything was rejected - never a
compliance conclusion (INV-5 in docs/document-pipeline-spec-v0.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

#: Page shapes produced by the router.
PAGE_TYPES = ("native_text", "native_table", "vector_chart", "pasted_image",
              "raster_scan", "mixed", "unknown")

#: Receipt status values.
STATUSES = ("extracted", "partially_extracted", "unreadable", "rejected_by_quality")

QUALITY_TIERS = ("good", "fair", "poor")


@dataclass(frozen=True)
class PageReceipt:
    page: int
    page_type: str
    quality_tier: str = "good"
    status: str = "extracted"
    dpi: float | None = None
    sharpness: float | None = None
    skew_deg: float | None = None
    native_chars: int = 0
    painted_images: int = 0
    chart_regions: int = 0
    table_regions: int = 0
    text_regions: int = 0
    reason_codes: tuple[str, ...] = ()
    attempts: tuple[dict, ...] = ()
    linked_fields: tuple[str, ...] = ()
    blind_spot: bool = False
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.page_type not in PAGE_TYPES:
            raise ValueError("unknown page type: " + str(self.page_type))
        if self.quality_tier not in QUALITY_TIERS:
            raise ValueError("unknown quality tier: " + str(self.quality_tier))
        if self.status not in STATUSES:
            raise ValueError("unknown page status: " + str(self.status))

    def as_dict(self) -> dict:
        return {"page": self.page, "page_type": self.page_type,
                "quality_tier": self.quality_tier, "status": self.status,
                "input_quality": {"dpi": self.dpi, "sharpness": self.sharpness,
                                  "skew_deg": self.skew_deg, "tier": self.quality_tier},
                "native_chars": self.native_chars, "painted_images": self.painted_images,
                "regions": {"chart": self.chart_regions, "table": self.table_regions,
                            "text": self.text_regions},
                "reason_codes": list(self.reason_codes),
                "attempts": [dict(a) for a in self.attempts],
                "linked_fields": list(self.linked_fields),
                "blind_spot": self.blind_spot,
                **({"extra": dict(self.extra)} if self.extra else {})}


_EXPONENT_MARK = r"\times"
_CARET = re.compile(r"\s*[×xX]\s*10\s*\^\s*\{?\s*(-?\d+)\s*\}?")
_EXPONENT_LOST = re.compile(r"\s*[×xX]\s*10\s*(?![0-9])")
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:e-?\d+|e\?)?")


def normalized_numbers(text: str) -> tuple[set[str], int]:
    """Return comparable numeric tokens and how many lost their exponent.

    A LaTeX form such as 4.92 x 10^{3} and a plain 4.92x10^3 both become
    4.92e3. A bare 4.92x10J (classic OCR dropping the superscript) counts as
    exponent loss and is never used as a value, so no magnitude is invented.
    """
    cleaned = str(text or "").replace("$", " ")
    cleaned = cleaned.replace(_EXPONENT_MARK, "×")
    cleaned = _CARET.sub(r"e\1", cleaned)
    cleaned = _EXPONENT_LOST.sub("e? ", cleaned)
    tokens, lost = set(), 0
    for match in _NUMBER.finditer(cleaned):
        token = match.group(0)
        if token.endswith("e?"):
            lost += 1
            continue
        tokens.add(token)
    return tokens, lost


def number_agreement(*channel_tokens: Iterable[str]) -> dict:
    """Compare numeric token sets across channels ordered by caller preference."""
    sets = [set(tokens) for tokens in channel_tokens]
    if not sets:
        return {"common": [], "per_channel_only": []}
    common = set.intersection(*sets)
    return {"common": sorted(common), "per_channel_only": [sorted(s - common) for s in sets]}


def is_vl_underread(*, vl_chars: int, ocr_lines: int, table_regions: int = 0,
                    text_regions: int = 0, figure_titles: int = 0) -> bool:
    """Detect the measured failure mode where PaddleOCR-VL returns almost nothing.

    On real scanned dailies the layout engine reported nine figures plus a table
    while the VL pass returned 66 characters against 266 classic OCR lines.
    Treating that as "the page has no text" would fabricate completeness.
    """
    layout_content = table_regions + text_regions + figure_titles
    if layout_content <= 0:
        return False
    if ocr_lines < 20:
        return False
    return vl_chars < max(200, ocr_lines * 8)
