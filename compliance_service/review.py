"""Review-as-labelling: three audited actions turn human review into gold."""

from __future__ import annotations

from datetime import datetime, timezone

ACTIONS = ("confirm", "correct", "reject")
REJECT_REASONS = ("UNREADABLE", "WRONG_TABLE", "WRONG_FIELD", "LOW_CONFIDENCE", "EXCEPTION")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_review(field_key: str, extracted, action: str, *, corrected=None, reason=None,
                 reviewer: str | None = None) -> dict:
    """Return one auditable review record (never mutates the run result)."""
    if action not in ACTIONS:
        raise ValueError("unknown review action: " + str(action))
    if action == "correct" and (corrected is None or str(corrected).strip() == ""):
        raise ValueError("correction requires a value")
    if action == "reject" and reason not in REJECT_REASONS:
        raise ValueError("reject requires a known reason")
    record = {"field_key": field_key, "extracted": extracted, "action": action,
              "reviewed_at": _now(), "reviewer": reviewer}
    if action == "correct":
        record["corrected"] = corrected
    if action == "reject":
        record["reason"] = reason
    return record


def gold_of(record: dict) -> dict:
    """The labelled truth implied by one review record."""
    action = record["action"]
    if action == "confirm":
        return {"field_key": record["field_key"], "value": record["extracted"], "verdict": "correct"}
    if action == "correct":
        return {"field_key": record["field_key"], "value": record["corrected"], "verdict": "corrected"}
    return {"field_key": record["field_key"], "value": None, "verdict": "rejected",
            "reason": record["reason"]}


def summarise(records) -> dict:
    counts = {action: 0 for action in ACTIONS}
    for record in records:
        counts[record["action"]] += 1
    total = sum(counts.values())
    return {"total": total, "counts": counts,
            "rejection_rate": round(counts["reject"] / total, 4) if total else 0.0}


def accuracy_against_gold(records) -> dict:
    """Precision of the automatic extraction on reviewed fields."""
    confirmed = sum(1 for r in records if r["action"] == "confirm")
    corrected = sum(1 for r in records if r["action"] == "correct")
    rejected = sum(1 for r in records if r["action"] == "reject")
    judged = confirmed + corrected + rejected
    return {"judged": judged, "correct": confirmed,
            "precision": round(confirmed / judged, 4) if judged else None,
            "rejected": rejected}

def layout_review(table, group: str, reader: str, *, mine_code=None, reviewer=None,
                  action: str = "confirm") -> dict:
    """Review-confirmed layout: returns the audit record plus the variant to store."""
    from .variants import register_variant
    if action not in {"confirm", "reject"}:
        raise ValueError("layout review action must be confirm or reject")
    variant = register_variant(table, group, reader, mine_code=mine_code,
                               notes="review-confirmed layout")
    record = {"kind": "layout", "group": group, "reader": reader,
              "variant_id": variant.variant_id,
              "header_signature": list(variant.header_signature),
              "action": action, "reviewed_at": _now(), "reviewer": reviewer}
    return {"record": record, "variant": variant}
