"""Local, read-only document extraction with explicit coverage and stable provenance.

No macros, hyperlinks, formulas or embedded objects are executed. OCR is deliberately
not inferred from an empty text layer: unsupported content remains visible as a gap.
"""

from datetime import date, datetime, time
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from time import perf_counter

from .contracts import RunPaused, StaleLease


def _block(result, text, kind, locator, metadata=None):
    identity = json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["blocks"].append({
        "block_id": sha256((result["content_hash"] + ":" + identity).encode("utf-8")).hexdigest(),
        "text": text, "kind": kind, "locator": locator, "metadata": metadata or {},
    })


def _coverage(result, unit_id, state, reason=None):
    item = {"unit_id": unit_id, "state": state}
    if reason:
        item["reason"] = reason
        issue = f"{unit_id}: {reason}"
        if issue not in result["issues"]:
            result["issues"].append(issue)
    result["coverage"].append(item)


def parse_document(path, *, file_id, progress_callback=None):
    """Extract a single immutable byte snapshot; file errors do not abort a batch."""
    path = Path(path)
    extension = path.suffix.lower().lstrip(".")
    result = {"file_id": file_id, "format": extension or "unknown", "content_hash": None,
              "blocks": [], "coverage": [], "issues": []}
    try:
        data = path.read_bytes()
        result["content_hash"] = sha256(data).hexdigest()
        if extension == "pdf":
            _pdf(data, result, progress_callback=progress_callback)
        elif extension == "docx":
            _docx(data, result)
        elif extension == "xlsx":
            _xlsx(data, result)
        elif extension in {"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"}:
            _coverage(result, "document", "unreadable", "image_requires_ocr")
        else:
            _coverage(result, "document", "unsupported", f"unsupported_format:{extension or 'unknown'}")
    except (StaleLease, RunPaused):
        raise
    except Exception as exc:
        # Keep exception classes for diagnosis, without leaking local paths or binary content.
        _coverage(result, "document", "failed", f"parse_failed:{type(exc).__name__}")
    return result


def _emit_progress(callback, **event):
    if callback is None:
        return
    callback(event)


def _pdf(data, result, *, progress_callback=None):
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted:
        _coverage(result, "document", "failed", "encrypted_pdf")
        return
    if not reader.pages:
        _coverage(result, "document", "empty", "pdf_has_no_pages")
    for number, page in enumerate(reader.pages, 1):
        started = perf_counter()
        unit = f"page:{number}"
        _emit_progress(progress_callback, stage="parse", event="page_started", file_id=result["file_id"],
                       page=number, page_total=len(reader.pages), status="running", issues=[])
        try:
            text = page.extract_text() or ""
            contents = page.get_contents()
            painted = _pdf_has_content(contents)
            has_images = _pdf_paints_images(contents, page.get("/Resources", {}), reader)
            if text.strip():
                _block(result, text, "page", {"page": number}, {"has_images": has_images})
            if has_images and text.strip():
                # Native text is still valid evidence. Image regions remain an
                # explicit gap and are eligible for OCR/VLM separately.
                _coverage(result, unit, "partial", "page_contains_image_requires_ocr")
            elif has_images:
                _coverage(result, unit, "unreadable", "page_image_requires_ocr")
            elif text.strip():
                _coverage(result, unit, "readable")
            elif painted:
                _coverage(result, unit, "unreadable", "nonempty_page_without_extractable_text")
            else:
                _coverage(result, unit, "empty")
        except (StaleLease, RunPaused):
            raise
        except Exception as exc:
            _coverage(result, unit, "failed", f"page_parse_failed:{type(exc).__name__}")
        _emit_progress(progress_callback, stage="parse", event="page_finished", file_id=result["file_id"],
                       page=number, page_total=len(reader.pages), status=result["coverage"][-1]["state"],
                       elapsed_ms=round((perf_counter() - started) * 1000),
                       issues=[result["coverage"][-1].get("reason")] if result["coverage"][-1].get("reason") else [])


def _pdf_paints_images(contents, resources, reader, seen=None):
    """Inspect only painted XObjects, including nested form streams.

    PDF generators commonly share one resource dictionary across every page;
    an unused image resource is not evidence that the page contains an image.
    """
    if contents is None:
        return False
    seen = set() if seen is None else seen
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    from pypdf.generic import ContentStream
    stream = contents if isinstance(contents, ContentStream) else ContentStream(contents, reader)
    for operands, operator in stream.operations:
        if operator == b"INLINE IMAGE":
            return True
        if operator != b"Do" or not operands:
            continue
        reference = objects.get(operands[0])
        if reference is None:
            continue
        key = (getattr(reference, "idnum", None), getattr(reference, "generation", None)) if hasattr(reference, "idnum") else id(reference)
        if key in seen:
            continue
        seen.add(key)
        obj = reference.get_object()
        if obj.get("/Subtype") == "/Image":
            return True
        if obj.get("/Subtype") == "/Form" and _pdf_paints_images(obj, obj.get("/Resources", resources), reader, seen):
            return True
    return False


def _pdf_has_content(contents):
    if contents is None:
        return False
    try:
        if hasattr(contents, "get_data"):
            return bool(contents.get_data().strip())
        return any(_pdf_has_content(item.get_object()) for item in contents)
    except Exception:
        return True


def _pdf_has_inline_image(contents):
    if contents is None:
        return False
    if hasattr(type(contents), "operations"):
        return any(operator == b"INLINE IMAGE" for _, operator in contents.operations)
    return any(_pdf_has_inline_image(item.get_object()) for item in contents)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _word_text(element):
    parts = []
    for node in element.iter():
        if node.tag in {_W + "t", _W + "delText"}:
            parts.append(node.text or "")
        elif node.tag == _W + "tab":
            parts.append("\t")
        elif node.tag in {_W + "br", _W + "cr"}:
            parts.append("\n")
    return "".join(parts)


def _word_gaps(element):
    tags = {node.tag.rsplit("}", 1)[-1] for node in element.iter()}
    reasons = []
    if tags & {"ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "tblPrChange", "tcPrChange"}:
        reasons.append("tracked_changes_unresolved")
    if tags & {"drawing", "pict", "object"}:
        reasons.append("embedded_object_requires_extraction")
    if tags & {"fldChar", "fldSimple", "altChunk"}:
        reasons.append("dynamic_or_external_content_unresolved")
    return reasons


def _docx(data, result):
    from docx import Document

    document = Document(BytesIO(data))
    paragraph_number = table_number = 0
    for element in document.element.body:
        if element.tag == _W + "p":
            paragraph_number += 1
            unit = f"paragraph:{paragraph_number}"
            text = _word_text(element)
            gaps = _word_gaps(element)
            if text:
                _block(result, text, "paragraph", {"paragraph": paragraph_number}, {"gaps": gaps})
            _coverage(result, unit, "unreadable" if gaps else "readable" if text.strip() else "empty",
                      ";".join(gaps) or None)
        elif element.tag == _W + "tbl":
            table_number += 1
            rows = []
            for row in element.findall(_W + "tr"):
                properties = row.find(_W + "trPr")
                before_node = properties.find(_W + "gridBefore") if properties is not None else None
                before = int(before_node.get(_W + "val", "0")) if before_node is not None else 0
                cells, merges = [""] * before, []
                for cell in row.findall(_W + "tc"):
                    text = "\n".join(_word_text(p) for p in cell.findall(_W + "p"))
                    properties = cell.find(_W + "tcPr")
                    span_node = properties.find(_W + "gridSpan") if properties is not None else None
                    span = int(span_node.get(_W + "val", "1")) if span_node is not None else 1
                    merge_node = properties.find(_W + "vMerge") if properties is not None else None
                    merges.append({"column": len(cells) + 1, "span": span,
                                   "vertical": merge_node.get(_W + "val", "continue") if merge_node is not None else None})
                    cells.append(text)
                    cells.extend([""] * (span - 1))
                rows.append((row, cells, merges))
            headers = rows[0][1] if rows else []
            for row_number, (row, cells, merges) in enumerate(rows, 1):
                gaps = _word_gaps(row)
                if row.findall(".//" + _W + "tbl"):
                    gaps.append("nested_table_unresolved")
                _block(result, "\t".join(cells), "table_row", {"table": table_number, "row": row_number},
                       {"cells": cells, "headers": headers, "merges": merges, "gaps": gaps})
                _coverage(result, f"table:{table_number}:row:{row_number}",
                          "unreadable" if gaps else "readable" if any(c.strip() for c in cells) else "empty",
                          ";".join(gaps) or None)
        elif element.tag != _W + "sectPr":
            _coverage(result, f"body:{len(result['coverage']) + 1}", "unreadable", "unsupported_body_structure")
    # Headers, footers, notes and comments are not silently treated as inspected body text.
    for part in document.part.package.parts:
        name = str(part.partname)
        if any(token in name for token in ("/header", "/footer", "/footnotes", "/endnotes")):
            from lxml import etree
            root = etree.fromstring(part.blob)
            if _word_text(root).strip() or _word_gaps(root):
                _coverage(result, name, "unreadable", "non_body_content_unresolved")
    if not result["coverage"]:
        _coverage(result, "document", "empty")


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _json_value(value):
    return value.isoformat() if isinstance(value, (datetime, date, time)) else value


def _xlsx(data, result):
    from openpyxl import load_workbook

    formulas = load_workbook(BytesIO(data), data_only=False, keep_links=False)
    cached = load_workbook(BytesIO(data), data_only=True, keep_links=False)
    try:
        for sheet in formulas:
            cached_sheet = cached[sheet.title]
            merged = [str(area) for area in sheet.merged_cells.ranges]
            rows = []
            sheet_gaps = []
            if sheet._images or sheet._charts:
                sheet_gaps.append("sheet_drawing_requires_extraction")
            for row in sheet.iter_rows():
                # Preserve real cells, including numeric zero and formulas; avoid manufactured blank rows.
                if not any(cell.value is not None for cell in row):
                    continue
                cells, details, row_gaps = [], [], []
                for cell in row:
                    value = cell.value
                    formula = value if cell.data_type == "f" else None
                    cache = cached_sheet[cell.coordinate].value if formula is not None else None
                    if formula is not None:
                        value = cache
                        if cache is None:
                            row_gaps.append(f"formula_without_cached_value:{cell.coordinate}")
                        elif cached_sheet[cell.coordinate].data_type == "e":
                            row_gaps.append(f"formula_cached_error:{cell.coordinate}")
                    elif cell.data_type == "e":
                        row_gaps.append(f"cell_error:{cell.coordinate}")
                    cells.append(_cell_text(value))
                    merge_range = next((area for area in sheet.merged_cells.ranges if cell.coordinate in area), None)
                    details.append({
                        "coordinate": cell.coordinate, "value": _json_value(value),
                        "raw_value": _json_value(cell.value), "formula": formula,
                        "cached_value": _json_value(cache), "data_type": cell.data_type,
                        "number_format": cell.number_format,
                        "row_hidden": bool(sheet.row_dimensions[cell.row].hidden),
                        "column_hidden": _column_hidden(sheet, cell.column),
                        "merged_range": str(merge_range) if merge_range is not None else None,
                        "merged_anchor": sheet.cell(merge_range.min_row, merge_range.min_col).coordinate if merge_range is not None else None,
                    })
                rows.append((row[0].row, cells, details, row_gaps))
            headers = rows[0][1] if rows else []
            for number, cells, details, gaps in rows:
                locator = {"sheet": sheet.title, "row": number}
                _block(result, "\t".join(cells), "sheet_row", locator,
                       {"cells": cells, "headers": headers, "cell_details": details,
                        "sheet_state": sheet.sheet_state, "row_hidden": bool(sheet.row_dimensions[number].hidden),
                        "merged_ranges": merged, "gaps": gaps})
                if gaps:
                    sheet_gaps.extend(gaps)
                    _coverage(result, f"sheet:{sheet.title}:row:{number}", "unreadable", ";".join(gaps))
            _coverage(result, f"sheet:{sheet.title}", "unreadable" if sheet_gaps else "readable" if rows else "empty",
                      ";".join(sheet_gaps) or None)
    finally:
        formulas.close()
        cached.close()


def _column_hidden(sheet, number):
    # Excel can store one grouped dimension for several columns.
    return any(dimension.hidden and (dimension.min or 0) <= number <= (dimension.max or 0)
               for dimension in sheet.column_dimensions.values())
