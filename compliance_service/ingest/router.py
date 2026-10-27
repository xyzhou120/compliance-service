"""Page-level routing driven by the document layout service.

The router answers one question per page: what is this page, and which channel
is allowed to read it. It never guesses text and never marks a page complete.
"""

from __future__ import annotations

from .receipt import PageReceipt

# Short-circuit order matters: a page that is mostly a chart with sparse labels
# must not be classified as scanned text (which used to send charts to OCR).
NATIVE_TEXT_MIN_CHARS = 200
IMAGE_AREA_SHARE = 0.15
SCAN_AREA_SHARE = 0.60
CHART_DRAWING_MIN = 20


def render_dpi_for(width_pt: float, height_pt: float, *, long_side: int = 2000,
                   minimum: int = 72, maximum: int = 200) -> int:
    """Pick a render DPI so the longest side stays within long_side pixels."""
    longest = max(float(width_pt or 0), float(height_pt or 0))
    if longest <= 0:
        return 150
    target = int(long_side * 72.0 / longest)
    return max(minimum, min(maximum, target))


def quality_tier(*, dpi: float | None, sharpness: float | None) -> str:
    """Grade capture quality; grades only limit conclusion strength."""
    if dpi is None and sharpness is None:
        return "fair"
    if dpi is not None and dpi < 120:
        return "poor"
    if sharpness is not None and sharpness < 100:
        return "poor"
    if dpi is not None and dpi >= 200 and (sharpness is None or sharpness >= 300):
        return "good"
    return "fair"


def classify_page(*, native_chars: int, painted_images: int = 0, drawings: int = 0,
                  image_area_share: float = 0.0, layout_regions: dict | None = None
                  ) -> tuple[str, tuple[str, ...]]:
    """Return (page_type, reason_codes)."""
    regions = layout_regions or {}
    charts = int(regions.get("chart") or 0)
    tables = int(regions.get("table") or 0)
    texts = int(regions.get("text") or 0)
    titles = int(regions.get("figure_title") or 0)
    reasons: list[str] = []

    sparse_text = native_chars < NATIVE_TEXT_MIN_CHARS
    if charts and sparse_text and (drawings >= CHART_DRAWING_MIN or charts >= 2):
        reasons.append("layout_reports_%d_chart_regions" % charts)
        return "vector_chart", tuple(reasons)

    if native_chars >= NATIVE_TEXT_MIN_CHARS and image_area_share < IMAGE_AREA_SHARE:
        if tables:
            reasons.append("native_text_with_%d_table_regions" % tables)
            return "native_table", tuple(reasons)
        return "native_text", ()

    if native_chars >= NATIVE_TEXT_MIN_CHARS:
        reasons.append("native_text_present_but_%.2f_image_area" % image_area_share)
        return "mixed", tuple(reasons)

    if painted_images:
        if image_area_share >= SCAN_AREA_SHARE:
            reasons.append("image_covers_%.2f_of_page" % image_area_share)
            return "raster_scan", tuple(reasons)
        return "pasted_image", tuple(reasons)

    if native_chars >= NATIVE_TEXT_MIN_CHARS:
        return "native_text", ()

    reasons.append("NO_TEXT_AND_NO_PAINTED_IMAGE")
    return "unknown", tuple(reasons)


def build_receipt(*, page: int, native_chars: int, painted_images: int = 0, drawings: int = 0,
                  image_area_share: float = 0.0, layout_regions: dict | None = None,
                  dpi: float | None = None, sharpness: float | None = None,
                  skew_deg: float | None = None) -> PageReceipt:
    """Assemble a receipt with the conservative status implied by its inputs."""
    regions = layout_regions or {}
    page_type, reasons = classify_page(native_chars=native_chars, painted_images=painted_images,
                                       drawings=drawings, image_area_share=image_area_share,
                                       layout_regions=regions)
    tier = quality_tier(dpi=dpi, sharpness=sharpness)
    if page_type == "unknown":
        status = "unreadable"
    elif tier == "poor":
        status = "rejected_by_quality"
        reasons = reasons + ("QUALITY_TIER_POOR",)
    elif page_type in {"pasted_image", "raster_scan", "mixed"}:
        status = "partially_extracted"
    else:
        status = "extracted"
    return PageReceipt(page=page, page_type=page_type, quality_tier=tier, status=status,
                       dpi=dpi, sharpness=sharpness, skew_deg=skew_deg,
                       native_chars=native_chars, painted_images=painted_images,
                       chart_regions=int(regions.get("chart") or 0),
                       table_regions=int(regions.get("table") or 0),
                       text_regions=int(regions.get("text") or 0),
                       reason_codes=reasons)
