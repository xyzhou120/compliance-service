"""Handwriting policy: never a numeric source, always reviewable."""

from __future__ import annotations

HANDWRITING_REASONS = ("HANDWRITTEN_REGION", "HANDWRITING_UNRESOLVED")


def classify(answer: dict) -> dict:
    """Normalise a vision answer about handwriting into an explicit policy."""
    answer = answer if isinstance(answer, dict) else {}
    present = bool(answer.get("handwriting_present"))
    readable = bool(answer.get("handwriting_readable"))
    reasons = []
    if present:
        reasons.append("HANDWRITTEN_REGION")
    if present and not readable:
        reasons.append("HANDWRITING_UNRESOLVED")
    return {"handwriting_present": present, "readable": readable,
            "numeric_allowed": False,
            "requires_review": present,
            "reasons": reasons}


def reject_numeric_from_handwriting(field: str) -> dict:
    """A numeric value that only exists as handwriting must not drive a verdict."""
    return {"field": field, "verified": False, "reason": "HANDWRITTEN_REGION"}
