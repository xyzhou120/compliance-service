"""Field archive: bind table cells to canonical fields with provenance.

The archive names which canonical fields matter for each material group. Binding
is deliberately strict: a value is bound only when its column header maps to a
known alias, and the cell text is kept exactly as read.
"""

from __future__ import annotations

from .canonical import CanonicalDocument, Locator
from .fact_extraction import FIELD_ALIASES
from .units import quantity

#: Canonical fields each material group is expected to provide.
GROUP_FIELDS = {
    "prevention_daily": ("trigger_type", "workface", "time", "stage", "method", "metric",
                         "point", "value", "unit", "action", "status", "immediate", "actor_role",
                         "target_scope", "route_conforms", "withdrawal_area", "expected_personnel",
                         "withdrawn_personnel", "event_time_reference"),
    "dispatch_daily": ("action", "time", "status", "actor_role", "work_content",
                       "attendance_count", "coal_cutting_count", "planned_cutting_count",
                       "cumulative_cutting_count"),
    "analysis": ("hazard_confirmed", "risk_grade", "signer", "signature_present", "signed_at",
                 "notified", "notification_time"),
    "release": ("action", "time", "approved", "signer"),
    "relief_acceptance": ("parameters", "acceptance_passed", "acceptance_signer",
                          "acceptance_time", "personnel_count", "unrelated_equipment_stopped"),
    "threshold": ("method", "metric", "point", "value", "unit", "approved", "valid_from",
                  "valid_to", "target_scope"),
    "design": ("relief_methods", "relief_parameters", "relief_area", "withdrawal_area",
               "max_personnel", "unrelated_equipment_stop_required", "technical_head", "approved",
               "valid_from", "valid_to", "target_scope"),
    "workface_risk": ("impact_risk", "risk_grade", "approved", "valid_from", "valid_to",
                      "target_scope"),
    "coal_risk": ("impact_risk", "risk_grade", "approved", "valid_from", "valid_to"),
}

CONTROL_FIELDS = ("action", "status", "actor_role", "target_scope", "immediate", "time",
                  "trigger_type", "workface", "hazard_confirmed")

_ALIAS_LOOKUP = {}
for canonical, aliases in FIELD_ALIASES.items():
    for alias in (canonical, *aliases):
        _ALIAS_LOOKUP.setdefault(_norm := str(alias).strip().replace(" ", ""), canonical)


def canonical_field(label, *, exact: bool = False) -> str | None:
    """Map a header label to a canonical field.

    exact=True only accepts an alias that equals the whole label, which is what
    plain-text binding needs: "全矿范围内 403109工作面" contains 工作面 but is not
    a workface column.
    """
    text = str(label or "").strip().replace(" ", "")
    if not text:
        return None
    if text in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[text]
    if exact:
        return None
    for alias, canonical in _ALIAS_LOOKUP.items():
        if alias and alias in text:
            return canonical
    return None


#: Identity columns are useful in every group, not only the ones that name them.
IDENTITY_FIELDS = ("workface", "time", "event_id", "event_time_reference")


def detect_header_index(rows, *, max_scan: int = 6) -> int:
    """Pick the row that carries the column labels (titles often sit above it)."""
    best_index, best_hits = 0, 0
    for index, row in enumerate(list(rows)[:max_scan]):
        hits = 0
        for cell in row:
            text = cell.text if hasattr(cell, "text") else str(cell)
            if canonical_field(str(text), exact=True):
                hits += 1
        if hits > best_hits:
            best_index, best_hits = index, hits
    return best_index if best_hits >= 2 else 0


def bind_table(table, group: str) -> list:
    """Return bound values: field, raw text, locator, header path, quantity."""
    allowed = set(GROUP_FIELDS.get(group, ())) | set(IDENTITY_FIELDS)
    paths = table.column_paths()
    if not allowed or not paths:
        return []
    columns = {}
    for index, path in enumerate(paths):
        for label in reversed(path):
            # Exact aliases only: substring matching turned 危险状态 into a status
            # column and would silently bind unrelated tables.
            field = canonical_field(label, exact=True)
            if field and field in allowed and field not in columns:
                columns.setdefault(field, index)
                break
    if not columns:
        return []
    unit_index = columns.get("unit")
    bound = []
    for row in table.data_rows():
        values = {}
        for field, index in columns.items():
            if index < len(row) and not row[index].empty:
                values[field] = row[index]
        if not values:
            continue
        # Identity columns alone (workface/time) are shared by every group; a row
        # with no group-specific field must not become a fact of this group.
        if not (set(values) & set(GROUP_FIELDS.get(group, ()))):
            continue
        unit_cell = row[unit_index] if unit_index is not None and unit_index < len(row) else None
        unit_text = unit_cell.text.strip() if unit_cell is not None and not unit_cell.empty else None
        for field, cell in values.items():
            entry = {"field": field, "raw": cell.text.strip(), "locator": cell.locator.as_dict(),
                     "header_path": list(paths[columns[field]]), "unit": None, "quantity": None}
            if field == "unit":
                entry["unit"] = cell.text.strip()
            if unit_text:
                entry["unit"] = unit_text
            if entry["unit"] and field in {"value", "max_personnel", "personnel_count",
                                            "expected_personnel", "withdrawn_personnel"}:
                converted = quantity(cell.text.strip(), entry["unit"])
                if converted:
                    entry["quantity"] = {"dimension": converted[0],
                                         "value": str(converted[1]),
                                         "scale": converted[2]}
            bound.append(entry)
    return bound


def bind_document(document: CanonicalDocument, group: str) -> list:
    bound = []
    for table in document.tables():
        for entry in bind_table(table, group):
            entry["file_id"] = document.file_id
            entry["table_id"] = table.table_id
            bound.append(entry)
    return bound


def evidence_id(locator: dict, field: str) -> str:
    """Deterministic evidence id so a bound value always resolves to its cell."""
    import hashlib
    import json
    payload = json.dumps({"locator": locator, "field": field}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return "cell-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

#: Which fact kind a material group produces (None = decide per row).
KIND_BY_GROUP = {
    "dispatch_daily": "action",
    "analysis": "action",
    "release": "action",
    "relief_acceptance": "action",
    "threshold": "threshold",
    "design": "design",
    "workface_risk": "risk_assessment",
    "coal_risk": "risk_assessment",
}

REQUIREMENT_GROUPS = frozenset({"threshold", "design", "workface_risk", "coal_risk"})


def fact_kind(group: str, values: dict) -> str:
    kind = KIND_BY_GROUP.get(group)
    if kind:
        return kind
    if values.get("action"):
        return "action"
    if values.get("stage") or values.get("value"):
        return "measurement"
    return "action"


def facts_from_document(document, group: str, *, table_selection: bool = False,
                        titles: dict | None = None) -> list:
    """Turn bound table values into facts shaped like the rule engine's input."""
    entries = bind_document(document, group)
    if table_selection:
        from .tables import select_table
        choice = select_table(document, group, titles=titles)
        if choice is None:
            return []
        entries = [entry for entry in entries if entry.get("table_id") == choice["table_id"]]
    rows = {}
    for entry in entries:
        locator = entry["locator"]
        key = (locator.get("file_id"), locator.get("table"), locator.get("row"))
        rows.setdefault(key, []).append(entry)
    facts = []
    for (file_id, table_id, row_index), entries in rows.items():
        values = {}
        unit = None
        quantities = {}
        for entry in entries:
            if entry["field"] == "unit":
                unit = entry["raw"]
                continue
            values[entry["field"]] = entry["raw"]
            if entry["quantity"]:
                quantities[entry["field"]] = entry["quantity"]
        if unit:
            values["unit"] = unit
        kind = fact_kind(group, values)
        evidence = [evidence_id(entry["locator"], entry["field"]) for entry in entries]
        fact = {"fact_id": "canonical-%s-t%s-r%s" % (file_id, table_id, row_index),
                "kind": kind, "event_id": values.get("event_id"),
                "workface": values.get("workface"), "time": values.get("time"),
                "source_role": "requirement" if group in REQUIREMENT_GROUPS else "actual_record",
                "evidence_refs": sorted(set(evidence)), "fields": values, "group": group}
        if quantities:
            fact["quantities"] = quantities
        facts.append(fact)
    return facts
