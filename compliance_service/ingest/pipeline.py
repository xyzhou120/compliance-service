"""Compose routing, receipts and channels into one page-level pipeline.

Everything here is deliberately conservative: a page that cannot be read yields a
receipt with a reason code instead of guessed text, and numeric values are only
"verified" when two independent channels agree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter

from .channels import ChannelUnavailable, RecognitionChannels
from dataclasses import replace

from .receipt import PageReceipt, is_vl_underread, number_agreement, normalized_numbers
from .router import build_receipt

ENV_FLAG = "COMPLIANCE_PAGE_RECEIPTS"

#: Which channels a page type is allowed to use.
CHANNEL_PLAN = {
    "native_text": ("native",),
    "native_table": ("native", "table_cells"),
    "vector_chart": ("chart",),
    "pasted_image": ("layout", "vl_ocr", "ocr"),
    "raster_scan": ("layout", "vl_ocr", "ocr"),
    "mixed": ("layout", "vl_ocr", "ocr"),
    "unknown": (),
}


def enabled(environ=None) -> bool:
    """Receipts are opt-in so the legacy pipeline keeps its behaviour by default."""
    source = os.environ if environ is None else environ
    return str(source.get(ENV_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


def plan_channels(page_type: str) -> tuple[str, ...]:
    return CHANNEL_PLAN.get(page_type, ())


def _page_units(parsed: dict):
    return [unit for unit in parsed.get("coverage") or [] if isinstance(unit, dict)]


def _chars_for_page(blocks, page: int) -> int:
    total = 0
    for block in blocks or []:
        locator = block.get("locator") or {}
        if isinstance(locator, dict) and locator.get("page") == page:
            total += len(str(block.get("text") or "").strip())
    return total


def build_page_receipts(parsed_files) -> list:
    """Static receipts from an already parsed document set (no network calls).

    The input is the (artifact, parsed) list produced by the source loader, so
    receipts can be attached even when the recognition service is unreachable.
    Region counts stay zero until a layout pass runs.
    """
    receipts = []
    for artifact, parsed in parsed_files:
        if not isinstance(artifact, dict):
            continue
        if isinstance(parsed, (str, Path)):
            # Spooled parses are stored as JSON files to bound memory.
            try:
                parsed = json.loads(Path(parsed).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                parsed = {"blocks": [], "coverage": [{"unit_id": "document",
                                                      "state": "partial",
                                                      "reason": "spooled_parse_unreadable"}]}
        if not isinstance(parsed, dict):
            continue
        file_id = artifact.get("file_id")
        blocks = parsed.get("blocks") or []
        units = _page_units(parsed) or [{"unit_id": "document", "state": "readable"}]
        for unit in units:
            unit_id = str(unit.get("unit_id") or "document")
            page = int(unit_id.split(":")[1]) if unit_id.startswith("page:") else 0
            state = str(unit.get("state") or "unknown")
            reason = str(unit.get("reason") or "")
            native_chars = _chars_for_page(blocks, page) if page else sum(
                len(str(b.get("text") or "")) for b in blocks)
            painted = 1 if "image" in reason else 0
            page_type = "native_text"
            if state in {"unreadable", "partial"}:
                page_type = "raster_scan" if painted else "unknown"
            receipt = build_receipt(page=page, native_chars=native_chars, painted_images=painted,
                                    image_area_share=1.0 if painted else 0.0)
            # The parser's own state is authoritative here: a short native page is
            # still native text, while an image-only page stays partially read.
            if state in {"readable", "empty"}:
                receipt = replace(receipt, page_type="native_text", status="extracted")
            elif painted:
                receipt = replace(receipt, page_type="raster_scan", status="partially_extracted")
            payload = receipt.as_dict()
            payload.update({"file_id": file_id, "unit_id": unit_id, "source_state": state,
                            "source_reason": reason})
            receipts.append(payload)
    return receipts


def verified_numbers(vl_text: str, ocr_text: str) -> dict:
    """Numbers a verdict may use: agreed by both channels and exponent-intact."""
    vl_tokens, vl_lost = normalized_numbers(vl_text)
    ocr_tokens, ocr_lost = normalized_numbers(ocr_text)
    agreement = number_agreement(vl_tokens, ocr_tokens)
    only_vl = agreement["per_channel_only"][0]
    only_ocr = agreement["per_channel_only"][1] if len(agreement["per_channel_only"]) > 1 else []
    return {"verified": agreement["common"], "only_vl": only_vl, "only_ocr": only_ocr,
            "exponent_lost": {"vl": vl_lost, "ocr": ocr_lost},
            "conflict": sorted(set(only_vl) & set(only_ocr))}


LAYOUT_TIMEOUT = 30.0
VL_TIMEOUT = 60.0
OCR_TIMEOUT = 45.0
VISION_TIMEOUT = 60.0
DEFAULT_PAGE_BUDGET = 90.0


def read_page(channels: RecognitionChannels, image: bytes, *, page: int,
              native_chars: int = 0, vision=None, file_id: str = "page",
              file_hash: str | None = None,
              budget_seconds: float = DEFAULT_PAGE_BUDGET, cropper=None) -> dict:
    """Run the allowed channels for one page and return blocks, numbers and receipt."""
    started = perf_counter()

    def over_budget():
        return budget_seconds is not None and (perf_counter() - started) > budget_seconds

    layout = channels.layout(image, timeout=LAYOUT_TIMEOUT)
    regions = {}
    for block in layout:
        if isinstance(block, dict) and block.get("label"):
            regions[block["label"]] = regions.get(block["label"], 0) + 1
    receipt = build_receipt(page=page, native_chars=native_chars, painted_images=1,
                            image_area_share=1.0, layout_regions=regions)
    attempts = [{"channel": "layout_detection", "regions": dict(regions)}]
    vl_blocks = channels.vl_ocr(image, timeout=VL_TIMEOUT)
    vl_text = "\n".join(str(b.get("content") or "") for b in vl_blocks if isinstance(b, dict))
    if over_budget():
        # Budget exhausted: stop here and degrade instead of stalling the batch.
        receipt = build_receipt(page=page, native_chars=native_chars, painted_images=1,
                                image_area_share=1.0, layout_regions=regions)
        receipt = PageReceipt(**{**receipt.__dict__, "status": "partially_extracted",
                                 "reason_codes": tuple(receipt.reason_codes) + ("PAGE_BUDGET_EXCEEDED",),
                                 "attempts": tuple(attempts)})
        return {"receipt": receipt.as_dict(), "regions": regions, "vl_text": vl_text,
                "ocr_text": "", "numbers": {"verified": [], "only_vl": [], "only_ocr": [],
                                            "exponent_lost": {"vl": 0, "ocr": 0}, "conflict": []},
                "attempts": attempts, "vl_blocks": vl_blocks}
    classic = channels.ocr(image, timeout=OCR_TIMEOUT)
    ocr_text = "\n".join(classic.get("rec_texts") or [])

    def still_underread(text):
        return is_vl_underread(vl_chars=len(text), ocr_lines=len(ocr_text.splitlines()),
                               table_regions=regions.get("table", 0),
                               text_regions=regions.get("text", 0),
                               figure_titles=regions.get("figure_title", 0))

    reasons = list(receipt.reason_codes)
    if still_underread(vl_text):
        attempts.append({"channel": "vl_ocr", "underread": True})
        retried = channels.vl_ocr_no_layout(image)
        retried_text = "\n".join(str(b.get("content") or "") for b in retried if isinstance(b, dict))
        attempts.append({"channel": "vl_ocr_no_layout", "chars": len(retried_text)})
        if len(retried_text) > len(vl_text):
            vl_text = retried_text
        if still_underread(vl_text):
            reasons.append("VL_UNDERREAD")
    # VL collapsed the table to a single cell: ask the vision model for an HTML
    # table, then judge it with the same gates. Never fabricate cells.
    table_blocks = [b for b in vl_blocks if isinstance(b, dict) and b.get("label") == "table"]
    degenerate = bool(regions.get("table")) and (
        not table_blocks or max((str(b.get("content") or "").count("<td") for b in table_blocks),
                                default=0) <= 1)
    if degenerate and vision is not None:
        # Try each detected table region separately: a full page mixes several
        # tables and the transcript then fails its gates.
        bboxes = [block.get("bbox") for block in layout
                  if isinstance(block, dict) and block.get("label") == "table"
                  and block.get("bbox")]
        table = diagnostics = None
        attempts_regions = bboxes[:2] if bboxes else [None]
        for bbox in attempts_regions:
            payload = image
            if cropper is not None and bbox is not None:
                try:
                    payload = cropper(image, bbox)
                except Exception:
                    payload = image
            table, diagnostics = vision.transcribe_table(payload, file_id=file_id, page=page,
                                                         file_hash=file_hash)
            attempts.append({"channel": "vision_table_region", "bbox": bbox,
                             "status": (diagnostics or {}).get("status")})
            if table is not None:
                break
        attempts.append({"channel": "vision_table", "status": diagnostics.get("status"),
                         "reasons": diagnostics.get("reasons", [])})
        if table is not None:
            vl_blocks = [b for b in vl_blocks if not (isinstance(b, dict) and b.get("label") == "table")]
            vl_blocks.append({"label": "table",
                              "content": "<table>" + "".join(
                                  "<tr>" + "".join("<td>%s</td>" % cell.text for cell in row) + "</tr>"
                                  for row in table.rows) + "</table>"})
            reasons.append("TABLE_FROM_VISION")
        else:
            reasons.append("VISION_TABLE_UNVERIFIED")
    numbers = verified_numbers(vl_text, ocr_text)
    if numbers["exponent_lost"]["ocr"]:
        reasons.append("EXPONENT_LOST_OCR")
    if reasons != list(receipt.reason_codes):
        status = receipt.status
        if "VL_UNDERREAD" in reasons and status == "extracted":
            status = "partially_extracted"
        receipt = PageReceipt(**{**receipt.__dict__, "status": status,
                                 "reason_codes": tuple(reasons), "attempts": tuple(attempts)})
    return {"receipt": receipt.as_dict(), "regions": regions, "vl_text": vl_text,
            "ocr_text": ocr_text, "numbers": numbers, "attempts": attempts,
            "vl_blocks": vl_blocks}


def page_trace(snapshot: dict) -> list:
    """Flat, human-readable per-page trace for diagnostics."""
    trace = []
    for receipt in snapshot.get("page_receipts") or []:
        if not isinstance(receipt, dict):
            continue
        reasons = list(receipt.get("reason_codes") or [])
        trace.append({
            "file_id": receipt.get("file_id"), "page": receipt.get("page"),
            "page_type": receipt.get("page_type"), "quality_tier": receipt.get("quality_tier"),
            "status": receipt.get("status"), "reasons": reasons,
            "summary": "第%s页：%s / %s / %s%s" % (
                receipt.get("page"), receipt.get("page_type"), receipt.get("quality_tier"),
                receipt.get("status"), ("（" + "；".join(reasons) + "）") if reasons else ""),
        })
    return trace
