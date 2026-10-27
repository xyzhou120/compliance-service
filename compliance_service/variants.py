"""Per-checklist layout variants.

The same checklist is laid out differently per mine: 胡家河's 临界指标 is a
蓝/黄/橙/红 grade matrix, while 文家坡's is 危险状态 a/b/c × 掘进/回采工作面 with
formula cells. A single generic reader cannot cover both, so each observed layout
is registered as a variant with its own reader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FORMULA_MARKERS = ("$", "\\\\times", "\\\\text", "\\\\frac", "≤", "≥", "<", ">")


@dataclass(frozen=True)
class Variant:
    variant_id: str
    group: str
    header_signature: tuple
    reader: str
    mine_code: str | None = None
    params: dict = field(default_factory=dict)
    notes: str = ""


#: Variants measured on real documents. Add one per newly observed layout.
SHIPPED = (
    Variant("hujiahe_threshold_grade", "threshold",
            ("应力在线预警模型", "掘进工作面", "蓝", "黄", "橙", "红"), "grade_matrix",
            notes="胡家河：色阶阈值矩阵"),
    Variant("wenjiapo_threshold_formula", "threshold",
            ("危险状态", "掘进工作面", "回采工作面"), "formula_table",
            notes="文家坡：危险状态 a/b/c × 工作面类型，单元格为公式"),
    Variant("wenjiapo_threshold_levels", "threshold",
            ("等级", "预警指标", "采取措施"), "formula_table",
            notes="文家坡：等级 + 预警指标条件 + 措施"),
    # Group-agnostic layouts seen on real documents; "*" means any material group.
    Variant("dispatch_ledger_columns", "*",
            ("序号", "采掘类别", "盘区名称", "工作面名称", "班次", "班长"), "field_columns",
            notes="调度台账：列名式记录表，无动作列时不产事实"),
    # Formula/grade readers only run for the groups that declared them: a wildcard
    # here leaked condition entries into unrelated groups (audit finding).
    Variant("danger_state_formula", "threshold",
            ("危险状态", "掘进工作面", "回采工作面"), "formula_table",
            notes="危险状态矩阵：单元格为公式，只作条件证据（仅 threshold）"),
    Variant("warning_level_matrix", "threshold",
            ("综合预警等级", "微震", "a", "地音", "a", "应力"), "grade_matrix",
            notes="综合预警等级矩阵：a/b/c 等级 × 指标（仅 threshold）"),
    Variant("hujiahe_threshold_rows", "threshold",
            ("危险等级", "回采工作面指标及其取值范围", "掘进工作面指标及其取值范围", "应对措施"),
            "grade_matrix",
            notes="胡家河视觉转写版：危险等级在行上（a/b/c/d），指标按工作面类型分列"),
)


def normalize_label(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def header_signature(table) -> tuple:
    if not table.rows:
        return ()
    return tuple(normalize_label(cell.text) for cell in table.rows[0] if not cell.empty)


def similarity(left, right) -> float:
    left, right = set(left), set(right)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def is_formula_cell(text) -> bool:
    raw = str(text or "")
    return any(marker in raw for marker in FORMULA_MARKERS)


def match_variant(table, group: str, *, mine_code: str | None = None,
                  variants=SHIPPED, threshold: float = 0.6):
    """Return the best matching variant for this table, or None."""
    signature = header_signature(table)
    if not signature:
        return None
    variants = SHIPPED if variants is None else variants
    best = None
    for variant in variants:
        if variant.group not in (group, "*"):
            continue
        if variant.mine_code and mine_code and variant.mine_code != mine_code:
            continue
        score = 1.0 if tuple(signature) == tuple(variant.header_signature) else similarity(
            signature, variant.header_signature)
        if score < threshold:
            continue
        if best is None or score > best["score"]:
            best = {"variant": variant, "score": round(score, 3),
                    "exact": tuple(signature) == tuple(variant.header_signature)}
    return best


def register_variant(table, group: str, reader: str, *, mine_code=None, notes="") -> Variant:
    """Create a variant record to store after a human confirms a new layout."""
    signature = header_signature(table)
    if not signature:
        raise ValueError("table has no header signature")
    slug = re.sub(r"[^a-z0-9]+", "_", str(mine_code or "unknown").lower()).strip("_")
    return Variant("%s_%s_%s" % (slug or "unknown", group, abs(hash(signature)) % (10 ** 6)),
                   group, signature, reader, mine_code=mine_code, notes=notes)


def bind_formula_table(table, group: str) -> list:
    """Read a condition/formula table: keep raw text, never a numeric value."""
    from .fields import GROUP_FIELDS, canonical_field, evidence_id as _eid
    allowed = set(GROUP_FIELDS.get(group, ()))
    paths = [list(path) for path in table.column_paths()]
    bound = []
    for row in table.data_rows():
        if not row:
            continue
        label = row[0].text.strip()
        for index, cell in enumerate(row[1:], start=1):
            if cell.empty:
                continue
            header = " / ".join(paths[index]) if index < len(paths) else ""
            field = canonical_field(header, exact=True) or canonical_field(header)
            if field not in allowed:
                # Unknown condition column: keep the text as evidence, not a field.
                field = None
            bound.append({"field": field or "condition", "raw": cell.text.strip(),
                          "locator": cell.locator.as_dict(), "header_path": paths[index]
                          if index < len(paths) else [], "unit": None, "quantity": None,
                          "source": "variant_formula", "label": label,
                          "formula": is_formula_cell(cell.text)})
    return bound
