"""Bind label/value pairs from plain OCR text (the photo-page fallback).

On photo pages the VL table parse can collapse to a single cell, while the
layout-free OCR block still carries the text. This module extracts conservative
label/value pairs from that text: a value is bound only when the label maps to a
canonical field and the value sits on the same line (or the immediately
following short line).
"""

from __future__ import annotations

import re

from .fields import canonical_field, evidence_id

_LABEL_SPLIT = re.compile(r"[:：=]")
_SPACE_SPLIT = re.compile(r"\s{2,}|\t+")
_NUMBER_AND_UNIT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([A-Za-z/％%‰一-龥]{0,8})$")
MAX_VALUE_LEN = 60
MIN_LABEL_LEN = 2
MAX_LABEL_LEN = 12


def lines_from_blocks(blocks, *, file_id: str, page: int) -> list:
    """Flatten OCR text blocks into located lines."""
    lines = []
    for block_index, block in enumerate(blocks or []):
        if not isinstance(block, dict):
            continue
        content = block.get("content")
        if content is None:
            content = "\n".join(block.get("rec_texts") or [])
        for line_index, raw in enumerate(str(content or "").splitlines()):
            text = raw.strip()
            if not text:
                continue
            lines.append({"text": text, "file_id": file_id, "page": page,
                          "block": block_index, "line": line_index,
                          "label": block.get("label")})
    return lines


def _match_label(text: str):
    """Return (field, label, value) using the longest whole-label match."""
    match = _LABEL_SPLIT.search(text)
    if match:
        label = text[:match.start()].strip()
        value = text[match.end():].strip()
        # Only whole, short labels count: matching inside prose produced values
        # like a full regulation paragraph being bound as "method".
        field = canonical_field(label, exact=True) if len(label) <= MAX_LABEL_LEN else None
        if field and value:
            return field, label, value
    tokens = [token for token in re.split(r"\s+", text) if token]
    for take in range(min(4, len(tokens)), 0, -1):
        label = " ".join(tokens[:take])
        field = canonical_field(label, exact=True)
        if field:
            return field, label, " ".join(tokens[take:]).strip()
    return None, None, None


def bind_lines(lines, group: str) -> list:
    """Return bound entries shaped like the table binder output."""
    from .fields import GROUP_FIELDS, IDENTITY_FIELDS
    allowed = set(GROUP_FIELDS.get(group, ())) | set(IDENTITY_FIELDS)
    bound = []
    for index, line in enumerate(lines or []):
        text = str(line.get("text") or "").strip()
        if len(text) < MIN_LABEL_LEN:
            continue
        field, label, value = _match_label(text)
        if field is None or field not in allowed:
            continue
        if value is None:
            # Value may sit on the next short line (common in vertical layouts).
            following = lines[index + 1] if index + 1 < len(lines or []) else None
            candidate = str((following or {}).get("text") or "").strip()
            if not candidate or len(candidate) > MAX_VALUE_LEN or canonical_field(candidate):
                continue
            value = candidate
        value = value[:MAX_VALUE_LEN].strip()
        if not value:
            continue
        locator = {"file_id": line.get("file_id"), "file_hash": None,
                   "page": line.get("page"), "table": None,
                   "row": line.get("line"), "column": None, "bbox": None}
        numeric = _NUMBER_AND_UNIT.match(value)
        unit = numeric.group(2) if numeric and numeric.group(2) else None
        entry = {"field": field, "raw": value, "locator": locator,
                 "header_path": [label], "unit": unit,
                 "quantity": None, "source": "text_line"}
        bound.append(entry)
    return bound


def facts_from_lines(lines, group: str, *, source_role: str | None = None) -> list:
    """Group bound text lines into one fact per block, with cell-level evidence."""
    entries = bind_lines(lines, group)
    if not entries:
        return []
    from .fields import REQUIREMENT_GROUPS, fact_kind
    grouped = {}
    for entry in entries:
        key = (entry["locator"].get("file_id"), entry["locator"].get("page"),
               lines[entry["locator"]["row"]]["block"] if entry["locator"].get("row") is not None
               and entry["locator"]["row"] < len(lines) else 0)
        grouped.setdefault(key, []).append(entry)
    facts = []
    for (file_id, page, block), items in grouped.items():
        values = {item["field"]: item["raw"] for item in items}
        unit = next((item["raw"] for item in items if item["field"] == "unit"), None)
        if unit:
            values["unit"] = unit
        role = source_role or ("requirement" if group in REQUIREMENT_GROUPS else "actual_record")
        facts.append({
            "fact_id": "text-%s-p%s-b%s" % (file_id, page, block),
            "kind": fact_kind(group, values), "event_id": values.get("event_id"),
            "workface": values.get("workface"), "time": values.get("time"),
            "source_role": role,
            "evidence_refs": sorted({evidence_id(item["locator"], item["field"]) for item in items}),
            "fields": values, "group": group, "source": "text_line"})
    return facts
