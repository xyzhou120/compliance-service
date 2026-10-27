"""Declarative table-reading schemas for checklists whose tables are matrices.

The real 冲击危险性预警临界指标 table is a grade matrix:

    应力在线预警模型 / 掘进工作面 | 蓝 | 黄 | 橙 | 红

That shape cannot be expressed with per-column field aliases, so a checklist may
declare how its table is read: which columns are the grade axis, which leading
columns are the row label, and which facts to emit.
"""

from __future__ import annotations

import re

from .fields import evidence_id

_NUMBER_UNIT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([A-Za-z/％%‰一-龥]{0,8})$")
PLACEHOLDERS = frozenset({"/", "—", "-", "无", "不适用", "N/A", "n/a", "／"})

#: Shipped schemas; later these move into the inspection configuration.
SCHEMAS = [
    {
        "schema_id": "threshold_color_grade",
        "group": "threshold",
        "title_keywords": ("临界指标", "预警指标", "冲击危险"),
        "grade_labels": ("蓝", "黄", "橙", "红"),
        "label_columns": 1,
        "unit": None,
        "emit": {"kind": "threshold", "value_field": "value",
                 "grade_field": "grade", "label_field": "metric"},
    },
    {
        "schema_id": "warning_level_abc",
        "group": "threshold",
        "title_keywords": ("预警", "等级"),
        "grade_labels": ("a", "b", "c", "d"),
        # 业务口径：以 c 为基准，c/d 均为超阈值，需立即撞人卸压；不同监测技术/矿不同，逐变体可覆盖
        "baseline_grade": "c",
        "over_threshold_grades": ("c", "d"),
        "immediate_action_grades": ("c", "d"),
        "label_columns": 1,
        "unit": None,
        "emit": {"kind": "threshold", "value_field": "value",
                 "grade_field": "grade", "label_field": "metric"},
    },
]


def _paths(table):
    return [list(path) for path in table.column_paths()]


def _grade_columns(table, schema):
    """Locate grade columns.

    Real matrices merge the leading label headers (colspan), so a data row can be
    narrower than the header row. Grades are always the trailing columns, so
    positions are computed from the right edge and shifted per row at bind time.
    """
    columns = []
    labels = [str(label).strip() for label in schema["grade_labels"]]
    for index, path in enumerate(_paths(table)):
        for label in path:
            if str(label).strip() in labels:
                columns.append((index, str(label).strip()))
                break
    width = table.column_count
    if len(columns) == len(labels) and [label for _, label in columns] != labels:
        columns = [(width - len(labels) + position, label)
                   for position, label in enumerate(labels)]
    return columns


def match_schema(table, title: str = "", schemas=None) -> dict | None:
    """Return the best schema for a table, or None when nothing matches."""
    best = None
    for schema in schemas or SCHEMAS:
        grades = _grade_columns(table, schema)
        if len(grades) < 2:
            continue
        title_hits = [word for word in schema.get("title_keywords", ()) if word in str(title or "")]
        score = len(grades) + 2 * len(title_hits)
        candidate = {"schema": schema, "score": score, "grade_columns": grades,
                     "title_hits": title_hits}
        if best is None or score > best["score"]:
            best = candidate
    return best


def grade_from_cell(text, schema) -> str | None:
    """Read a grade from wording like "c.中等冲击危险" as well as a bare "c"."""
    import re as _re
    value = str(text or "").strip()
    labels = {str(label).strip().lower() for label in schema.get("grade_labels", ())}
    if value.lower() in labels:
        return value.lower()
    match = _re.match(r"^([abcd])\b", value) or _re.match(r"^([abcd])[.、:：]", value)
    if match:
        return match.group(1)
    for word, grade in (("无冲击", "a"), ("弱冲击", "b"), ("中等冲击", "c"), ("强冲击", "d")):
        if word in value and grade in labels:
            return grade
    return None


def _row_grade_columns(table, schema):
    """Detect row-oriented grade matrices: 等级 in the first cell of each row."""
    hits = 0
    for row in table.data_rows():
        if row and grade_from_cell(row[0].text, schema):
            hits += 1
    return hits >= 2


def bind_schema(table, schema, *, title: str = "") -> list:
    """Read a grade-matrix table into bound values keyed by grade."""
    match = match_schema(table, title, schemas=[schema])
    if match is None and _row_grade_columns(table, schema):
        return _bind_row_grades(table, schema)
    if match is None:
        return []
    label_columns = int(schema.get("label_columns", 1))
    bound = []
    width = table.column_count
    for row in table.data_rows():
        labels = [row[index].text.strip() for index in range(min(label_columns, len(row)))
                  if not row[index].empty]
        if not labels:
            continue
        label = " / ".join(labels)
        offset = width - len(row)
        for header_index, grade in match["grade_columns"]:
            index = header_index - offset
            if index < 0 or index >= len(row) or row[index].empty:
                continue
            raw = row[index].text.strip()
            if raw in PLACEHOLDERS:
                continue
            numeric = _NUMBER_UNIT.match(raw)
            unit = (numeric.group(2) if numeric and numeric.group(2) else None) or schema.get("unit")
            value = numeric.group(1) if numeric else raw
            bound.append({"schema_id": schema["schema_id"], "label": label, "grade": grade,
                          "raw": raw, "value": value, "unit": unit,
                          "locator": row[index].locator.as_dict(),
                          "header_path": _paths(table)[header_index]
                          if header_index < len(_paths(table)) else [],
                          "source": "schema"})
    return bound


def _bind_row_grades(table, schema) -> list:
    """Row-oriented matrix: first cell is the grade, remaining columns are metrics."""
    paths = _paths(table)
    bound = []
    for row in table.data_rows():
        if not row:
            continue
        grade = grade_from_cell(row[0].text, schema)
        if not grade:
            continue
        for index in range(1, len(row)):
            cell = row[index]
            if cell.empty or cell.text.strip() in PLACEHOLDERS:
                continue
            header = " / ".join(paths[index]) if index < len(paths) else ""
            bound.append({"schema_id": schema["schema_id"], "label": header or ("列%d" % index),
                          "grade": grade, "raw": cell.text.strip(),
                          "value": cell.text.strip(), "unit": schema.get("unit"),
                          "locator": cell.locator.as_dict(), "header_path": paths[index]
                          if index < len(paths) else [], "source": "schema_rows"})
    return bound


def facts_from_schema(table, schema, *, group: str | None = None, title: str = "",
                        rule: dict | None = None) -> list:
    """Emit one fact per (row label, grade) pair with cell-level evidence.

    A rule read from the document itself (per-mine standard) wins over the
    variant default; without either, grade facts stay unresolved.
    """
    from .threshold_rule import apply_rule
    schema = apply_rule(schema, rule)
    emit = schema.get("emit") or {}
    facts = []
    for entry in bind_schema(table, schema, title=title):
        fields = {emit.get("label_field", "metric"): entry["label"],
                  emit.get("grade_field", "grade"): entry["grade"],
                  emit.get("value_field", "value"): entry["value"]}
        if entry["unit"]:
            fields["unit"] = entry["unit"]
        if emit.get("grade_field"):
            fields["baseline_source"] = schema.get("baseline_source", "unset")
        baseline = schema.get("baseline_grade")
        if baseline:
            from .grades import at_or_above
            reached = at_or_above(entry["grade"], baseline)
            if reached is not None:
                fields["over_threshold"] = reached
                fields["baseline_grade"] = baseline
                if reached and entry["grade"] in tuple(schema.get("immediate_action_grades") or ()):
                    fields["immediate_action_required"] = True
        facts.append({
            "fact_id": "schema-%s-%s-%s" % (table.table_id, entry["grade"], abs(hash(entry["label"])) % (10 ** 8)),
            "kind": emit.get("kind", "threshold"),
            "event_id": None, "workface": None, "time": None,
            "source_role": "requirement",
            "evidence_refs": [evidence_id(entry["locator"], emit.get("value_field", "value"))],
            "fields": fields, "group": group or schema.get("group"),
            "schema_id": schema["schema_id"], "source": "schema"})
    return facts


def facts_for_document(document, *, titles=None, schemas=None) -> list:
    """Apply the first matching schema to every table in a document."""
    titles = titles or {}
    facts = []
    for table in document.tables():
        match = match_schema(table, titles.get(table.table_id, ""), schemas=schemas)
        if match is None:
            continue
        facts.extend(facts_from_schema(table, match["schema"],
                                       title=titles.get(table.table_id, "")))
    return facts

#: How each material group's table is read.
GROUP_READERS = {
    "prevention_daily": "field_columns",
    "dispatch_daily": "field_columns",
    "analysis": "field_columns",
    "release": "field_columns",
    "relief_acceptance": "field_columns",
    "threshold": "grade_matrix",
    "design": "label_value",
    "workface_risk": "label_value",
    "coal_risk": "label_value",
}


def reader_for(group: str) -> str:
    return GROUP_READERS.get(group, "field_columns")


def bind_label_value(table, group: str) -> list:
    """Read a two-column table: label cell on the left, value cell on the right."""
    from .fields import GROUP_FIELDS, IDENTITY_FIELDS, canonical_field, evidence_id as _eid
    allowed = set(GROUP_FIELDS.get(group, ())) | set(IDENTITY_FIELDS)
    bound = []
    paths = _paths(table)
    # Label/value tables have no header row: every row is a pair.
    for row in table.rows:
        if len(row) < 2:
            continue
        label_cell, value_cell = row[0], row[1]
        field = canonical_field(label_cell.text, exact=True)
        if field is None or field not in allowed or value_cell.empty:
            continue
        bound.append({"field": field, "raw": value_cell.text.strip(),
                      "locator": value_cell.locator.as_dict(),
                      "header_path": paths[0] if paths else [],
                      "unit": None, "quantity": None, "source": "label_value"})
    return bound


def read_table(table, group: str, *, title: str = "", mine_code: str | None = None,
               variants=None) -> list:
    """Read one table: a registered layout variant wins, otherwise the group default."""
    from .fields import bind_table
    from .variants import bind_formula_table, match_variant
    variant = match_variant(table, group, mine_code=mine_code, variants=variants)
    mode = variant["variant"].reader if variant else reader_for(group)
    if mode == "grade_matrix":
        match = match_schema(table, title)
        if match:
            return bind_schema(table, match["schema"], title=title)
        # Row-oriented matrices have no grade columns: try every schema's row reader.
        for schema in SCHEMAS:
            entries = bind_schema(table, schema, title=title)
            if entries:
                return entries
        # Not a grade matrix: a plain method/metric/point/value table is still a
        # threshold table and is read by columns.
        return bind_table(table, group)
    if mode == "formula_table":
        return bind_formula_table(table, group)
    if mode == "label_value":
        return bind_label_value(table, group)
    return bind_table(table, group)


def reader_choice(table, group: str, *, title: str = "", mine_code: str | None = None,
                  variants=None) -> dict:
    """Which reader would handle this table (for diagnostics and review)."""
    from .variants import match_variant
    variant = match_variant(table, group, mine_code=mine_code, variants=variants)
    if variant:
        return {"mode": variant["variant"].reader, "variant_id": variant["variant"].variant_id,
                "score": variant["score"], "exact": variant["exact"]}
    return {"mode": reader_for(group), "variant_id": None, "score": None, "exact": None}
