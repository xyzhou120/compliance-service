"""Offline baseline: route pages, call the shared channels, print the evidence table.

Usage:
    COMPLIANCE_OCR_URL=http://172.26.1.84:7000 python scripts/page_channel_baseline.py \
        --pdf samples/a.pdf:1 --pdf samples/b.pdf:1-3 [--json out.json]

Requires pdftoppm (poppler) for rendering and the private recognition service.
Nothing here decides compliance; it only measures what the channels can read.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from compliance_service.ingest import (RecognitionChannels, build_receipt, is_vl_underread,
                                       number_agreement, normalized_numbers, render_dpi_for)


def parse_pages(value: str) -> tuple[Path, list[int]]:
    path, _, spec = value.rpartition(":")
    if not path:
        path, spec = spec, "1"
    pdf = Path(path).expanduser()
    if not pdf.is_file():
        raise SystemExit("missing pdf: " + str(pdf))
    pages: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            pages.extend(range(int(start), int(end) + 1))
        elif part:
            pages.append(int(part))
    return pdf, pages or [1]


def render(pdf: Path, page: int, renderer: str) -> bytes:
    with TemporaryDirectory(prefix="baseline-") as directory:
        target = Path(directory) / "page"
        completed = subprocess.run([renderer, "-f", str(page), "-l", str(page), "-r", "170",
                                    "-singlefile", "-png", str(pdf), str(target)],
                                   capture_output=True, timeout=90, check=False)
        output = target.with_suffix(".png")
        if completed.returncode or not output.is_file():
            raise SystemExit("render failed for %s p%d" % (pdf.name, page))
        return output.read_bytes()


def table_rows(blocks) -> tuple[int, int]:
    cells = spans = 0
    for block in blocks or []:
        if not isinstance(block, dict) or block.get("label") != "table":
            continue
        content = block.get("content") or ""
        cells += content.count("<td")
        spans += content.count("colspan") + content.count("rowspan")
    return cells, spans


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Page-channel baseline")
    parser.add_argument("--pdf", action="append", required=True, help="path[:pages] e.g. a.pdf:1-3")
    parser.add_argument("--json", help="write the full result table here")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--renderer", default=os.environ.get("COMPLIANCE_PDF_RENDERER") or "pdftoppm")
    args = parser.parse_args(argv)

    base_url = os.environ.get("COMPLIANCE_OCR_URL")
    if not base_url:
        raise SystemExit("COMPLIANCE_OCR_URL is required")
    channels = RecognitionChannels(base_url, concurrency=args.concurrency)

    rows = []
    for spec in args.pdf:
        pdf, pages = parse_pages(spec)
        for page in pages:
            image = render(pdf, page, args.renderer)
            started = perf_counter()
            layout = channels.layout(image)
            t_layout = perf_counter() - started
            started = perf_counter()
            vl_blocks = channels.vl_ocr(image)
            t_vl = perf_counter() - started
            started = perf_counter()
            classic = channels.ocr(image)
            t_ocr = perf_counter() - started
            regions = {}
            for block in layout:
                if isinstance(block, dict) and block.get("label"):
                    regions[block["label"]] = regions.get(block["label"], 0) + 1
            # Rendered page image: no native text layer on this path, so the
            # router classifies from the layout regions alone.
            receipt = build_receipt(page=page, native_chars=0, painted_images=0,
                                    layout_regions=regions)
            vl_text = "\n".join(str(b.get("content") or "") for b in vl_blocks if isinstance(b, dict))
            ocr_text = "\n".join(classic.get("rec_texts") or [])
            vl_numbers, vl_lost = normalized_numbers(vl_text)
            ocr_numbers, ocr_lost = normalized_numbers(ocr_text)
            cells, spans = table_rows(vl_blocks)
            agreement = number_agreement(vl_numbers, ocr_numbers)
            rows.append({
                "file": pdf.name, "page": page, "regions": regions,
                "page_type": receipt.page_type, "quality_tier": receipt.quality_tier,
                "vl_chars": len(vl_text), "ocr_lines": len(ocr_text.splitlines()),
                "cells": cells, "spans": spans,
                "numbers_common": len(agreement["common"]),
                "numbers_only_vl": len(agreement["per_channel_only"][0]) if agreement["per_channel_only"] else 0,
                "numbers_only_ocr": len(agreement["per_channel_only"][1]) if len(agreement["per_channel_only"]) > 1 else 0,
                "exponent_lost": {"vl": vl_lost, "ocr": ocr_lost},
                "vl_underread": is_vl_underread(vl_chars=len(vl_text), ocr_lines=len(ocr_text.splitlines()),
                                                table_regions=regions.get("table", 0),
                                                text_regions=regions.get("text", 0),
                                                figure_titles=regions.get("figure_title", 0)),
                "seconds": {"layout": round(t_layout, 2), "vl": round(t_vl, 2), "ocr": round(t_ocr, 2)},
            })
            print("%-34s p%-3d %-13s %-6s vl=%-5d ocr=%-4d cells=%-4d common=%-4d under=%s %.1f/%.1f/%.1f" % (
                pdf.name[:34], page, receipt.page_type, receipt.quality_tier, len(vl_text),
                len(ocr_text.splitlines()), cells, len(agreement["common"]),
                rows[-1]["vl_underread"], t_layout, t_vl, t_ocr), flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
