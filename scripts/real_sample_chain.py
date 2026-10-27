"""Real-sample acceptance chain: page -> table -> fields -> facts -> grade threshold.

Usage:
    COMPLIANCE_OCR_URL=http://host:7000 python scripts/real_sample_chain.py \
        --image /tmp/pages/*.png --group threshold --group dispatch_daily \
        [--vision-limit 3] [--json out.json]

PDF input is rendered with pdftoppm (poppler). Images are used as-is.
The script prints one line per page and a summary; it never decides compliance.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from compliance_service.canonical import document_from_blocks
from compliance_service.ingest import RecognitionChannels, read_page
from compliance_service.table_schema import read_table

GROUPS = ("prevention_daily", "dispatch_daily", "analysis", "release", "relief_acceptance",
          "threshold", "design", "workface_risk", "coal_risk")


def render_pdf(pdf: Path, pages, renderer: str) -> list:
    images = []
    for page in pages:
        with TemporaryDirectory(prefix="chain-") as directory:
            target = Path(directory) / "page"
            completed = subprocess.run(
                [renderer, "-f", str(page), "-l", str(page), "-r", "170", "-singlefile", "-png",
                 str(pdf), str(target)], capture_output=True, timeout=120, check=False)
            output = target.with_suffix(".png")
            if completed.returncode or not output.is_file():
                raise SystemExit("render failed: %s p%s" % (pdf.name, page))
            images.append((pdf.name + "#p" + str(page), output.read_bytes()))
    return images


def parse_pages(spec: str) -> list:
    pages = []
    for part in spec.split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            pages.extend(range(int(start), int(end) + 1))
        else:
            pages.append(int(part))
    return pages or [1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="real sample chain")
    parser.add_argument("--image", action="append", default=[], help="glob of page images")
    parser.add_argument("--pdf", action="append", default=[], help="path[:pages]")
    parser.add_argument("--group", action="append", choices=GROUPS, default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--vision-limit", type=int, default=0,
                        help="max pages that may trigger a cropped vision table")
    parser.add_argument("--json")
    parser.add_argument("--renderer", default=os.environ.get("COMPLIANCE_PDF_RENDERER") or "pdftoppm")
    args = parser.parse_args(argv)

    base_url = os.environ.get("COMPLIANCE_OCR_URL")
    if not base_url:
        raise SystemExit("COMPLIANCE_OCR_URL is required")
    groups = args.group or ["threshold"]

    pages = []
    for pattern in args.image:
        for path in sorted(glob.glob(pattern)):
            pages.append((Path(path).name, Path(path).read_bytes()))
    for spec in args.pdf:
        pdf_part, _, page_part = spec.rpartition(":")
        pdf = Path(pdf_part or page_part)
        pages.extend(render_pdf(pdf, parse_pages(page_part if pdf_part else "1"), args.renderer))
    if args.limit:
        pages = pages[: args.limit]
    if not pages:
        raise SystemExit("no pages to process")

    channels = RecognitionChannels(base_url, concurrency=2)
    vision = None
    if args.vision_limit:
        from compliance_service.vlm import VisionChannel
        vision = VisionChannel()

    rows = []
    vision_used = 0
    for index, (name, image) in enumerate(pages, start=1):
        use_vision = vision if (vision is not None and vision_used < args.vision_limit) else None
        outcome = read_page(channels, image, page=index, file_id=name, vision=use_vision)
        if use_vision is not None and any(a.get("channel") == "vision_table" for a in outcome["attempts"]):
            vision_used += 1
        doc = document_from_blocks(outcome["vl_blocks"], file_id=name, page=index)
        tables = doc.tables()
        row = {"page": name, "receipt": outcome["receipt"], "tables": len(tables),
               "facts": {}, "readers": {}}
        for group in groups:
            entries = []
            for table in tables:
                entries.extend(read_table(table, group))
            row["readers"][group] = len(entries)
            row["facts"][group] = len(entries)
        print("%-38s %-13s %-9s tables=%d %s reasons=%s" % (
            name[:38], outcome["receipt"]["page_type"], outcome["receipt"]["status"], len(tables),
            " ".join("%s=%d" % (group, count) for group, count in row["facts"].items()),
            outcome["receipt"]["reason_codes"]), flush=True)
        rows.append(row)

    summary = {"pages": len(rows),
               "pages_with_tables": sum(1 for row in rows if row["tables"]),
               "facts": {group: sum(row["facts"][group] for row in rows) for group in groups},
               "status": {}}
    for row in rows:
        status = row["receipt"]["status"]
        summary["status"][status] = summary["status"].get(status, 0) + 1
    print("\n== summary ==")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "summary": summary},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
