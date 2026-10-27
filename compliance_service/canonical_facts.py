"""Bridge canonical facts into the rule engine snapshot.

The legacy extractor produces text-line facts; the canonical layer produces
table/field facts with cell provenance. This module turns the latter into facts
the rule engine accepts, keyed by the material group recorded on each file.
"""

from __future__ import annotations

from .canonical import Cell, Locator, Table
from .table_schema import read_table


def _cells_from_evidence(evidence):
    """Rebuild one table per evidence row group (xlsx/docx table rows)."""
    rows = {}
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata") or {}
        cells, headers = metadata.get("cells"), metadata.get("headers")
        if not isinstance(cells, list) or not isinstance(headers, list) or not headers:
            continue
        locator = entry.get("locator") or {}
        key = (entry.get("file_id"), locator.get("sheet"), locator.get("table"))
        rows.setdefault(key, {"headers": headers, "rows": []})
        rows[key]["rows"].append((entry.get("evidence_id"), locator, cells))
    tables = []
    for index, ((file_id, _sheet, _table), payload) in enumerate(sorted(rows.items(), key=lambda item: str(item[0]))):
        headers = payload["headers"]
        locator_row = payload["rows"][0][1] or {}
        table_cells = []
        header_cells = [Cell(text=str(value or ""), locator=Locator(file_id=str(file_id),
                                                                  page=locator_row.get("page")))
                        for value in headers]
        table_cells.append(tuple(header_cells))
        for evidence_id, locator, cells in payload["rows"]:
            row_cells = [Cell(text=str(cells[i] if i < len(cells) else ""),
                              locator=Locator(file_id=str(file_id), page=(locator or {}).get("page"),
                                              row=(locator or {}).get("row")))
                         for i in range(len(headers))]
            row_cells_tuple = tuple(row_cells)
            table_cells.append(row_cells_tuple)
        tables.append(Table(table_id="ev-%d" % index, rows=tuple(table_cells),
                            locator=Locator(file_id=str(file_id)), header_rows=1))
    return tables


def _evidence_refs_by_row(evidence, file_id):
    refs = {}
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        locator = entry.get("locator") or {}
        if str(entry.get("file_id")) != str(file_id):
            continue
        key = (locator.get("page"), locator.get("row"))
        refs.setdefault(key, []).append(entry.get("evidence_id"))
    return refs


def facts_for_group(evidence, group, *, file_hash=None) -> list:
    """Rows bound by the group's reader become facts with row-level evidence."""
    facts = []
    for table in _cells_from_evidence(evidence):
        entries = read_table(table, group)
        if not entries:
            continue
        refs = _evidence_refs_by_row(evidence, table.locator.file_id)
        by_row = {}
        for entry in entries:
            row = (entry["locator"] or {}).get("row")
            by_row.setdefault(row, []).append(entry)
        for row, items in by_row.items():
            values = {item["field"]: item["raw"] for item in items}
            if not values:
                continue
            refs_for_row = sorted({ref for ref in refs.get((table.locator.page, row), []) if ref})
            if not refs_for_row:
                # fall back to any evidence of the same file so the fact stays cited
                refs_for_row = sorted({ref for values_ in refs.values() for ref in values_ if ref})[:1]
            facts.append({
                "fact_id": "canon-%s-%s-%s" % (table.table_id, group, row),
                "kind": _kind_for(group, values),
                "event_id": values.get("event_id"),
                "workface": values.get("workface"),
                "time": values.get("time"),
                "source_role": "requirement" if group in {"threshold", "design",
                                                          "workface_risk", "coal_risk"} else "actual_record",
                "evidence_refs": refs_for_row,
                "fields": values, "source": "canonical", "material_group": group})
    return facts


def _kind_for(group, values):
    from .fields import fact_kind
    return fact_kind(group, values)


def merge_into_snapshot(snapshot) -> dict:
    """Append canonical facts (per file material group) to the snapshot facts."""
    files = {str(f.get("file_id")): f for f in snapshot.get("files") or [] if isinstance(f, dict)}
    extra = []
    for file_id, file in files.items():
        group = file.get("material_group")
        if not group:
            continue
        group_evidence = [e for e in snapshot.get("evidence") or []
                          if isinstance(e, dict) and str(e.get("file_id")) == file_id]
        extra.extend(facts_for_group(group_evidence, group))
    if not extra:
        return snapshot
    known = {(f.get("fact_id") for f in snapshot.get("facts") or [])}
    merged = list(snapshot.get("facts") or []) + [f for f in extra if f["fact_id"] not in known]
    return {**snapshot, "facts": merged, "canonical_fact_count": len(extra)}
