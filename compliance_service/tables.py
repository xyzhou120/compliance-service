"""Pick the table that carries a group's fields, instead of guessing.

A daily report holds several tables (statistics matrix, progress table, analysis
table). Only one of them carries the canonical fields. Selection is scored from
header hits and title keywords; low scores stay unselected rather than binding a
wrong column.
"""

from __future__ import annotations

from .fields import canonical_field, GROUP_FIELDS

#: Title keywords that identify the table holding a group's fields.
TITLE_HINTS = {
    "prevention_daily": ("监测", "预警", "日报", "防冲"),
    "dispatch_daily": ("调度", "台账", "日报"),
    "analysis": ("分析", "研判"),
    "release": ("通知", "放行", "复工"),
    "relief_acceptance": ("验收", "钻孔"),
    "threshold": ("临界指标", "预警指标", "阈值"),
    "design": ("设计", "措施", "方案"),
    "workface_risk": ("评价", "危险性"),
    "coal_risk": ("评价", "危险性", "煤层"),
}

MIN_SCORE = 2


def score_table(table, group: str, *, title: str = "") -> dict:
    """Score one table for a group: header alias hits plus title keywords."""
    allowed = set(GROUP_FIELDS.get(group, ()))
    hits = set()
    for path in table.column_paths():
        for label in path:
            field = canonical_field(label)
            if field and field in allowed:
                hits.add(field)
                break
    title_hits = [word for word in TITLE_HINTS.get(group, ()) if word and word in str(title or "")]
    score = len(hits) + 2 * len(title_hits)
    return {"table_id": table.table_id, "score": score, "fields": sorted(hits),
            "title_hits": title_hits, "columns": len(table.column_paths())}


def select_table(document, group: str, *, titles=None):
    """Return the best table for a group, or None when nothing scores high enough."""
    titles = titles or {}
    scored = [score_table(table, group, title=titles.get(table.table_id, ""))
              for table in document.tables()]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item["score"], item["table_id"]))
    best = scored[0]
    if best["score"] < MIN_SCORE:
        return None
    best["ambiguous"] = len(scored) > 1 and scored[1]["score"] == best["score"]
    return best


def select_for_document(document, group: str, *, titles=None):
    """Convenience: return the selected Table object plus its score payload."""
    choice = select_table(document, group, titles=titles)
    if choice is None:
        return None, None
    for table in document.tables():
        if table.table_id == choice["table_id"]:
            return table, choice
    return None, None
