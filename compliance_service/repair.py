"""Repair one table region with a bounded vision call.

Only suspicious tables are repaired (header looks like data), and every attempt is
charged to a budget so a slow model can never stall a batch.
"""

from __future__ import annotations

from time import perf_counter

from .imageops import crop_region


def repair_region(vision, budget, image: bytes, region_bbox, *, file_id: str, page,
                  upscale: float = 1.2, timeout_seconds: float = 60.0) -> dict:
    """Return {"table": Table|None, "status": str, "seconds": float}."""
    allowed, reason = budget.allow(page)
    if not allowed:
        budget.skip(page, reason)
        return {"table": None, "status": "VISION_BUDGET_SKIPPED", "reason": reason, "seconds": 0.0}
    started = perf_counter()
    try:
        cropped = crop_region(image, region_bbox, upscale=upscale)
    except ValueError as exc:
        budget.skip(page, "CROP_FAILED")
        return {"table": None, "status": "CROP_FAILED", "reason": str(exc), "seconds": 0.0}
    elapsed = perf_counter() - started
    if elapsed > timeout_seconds:
        budget.skip(page, "CROP_TIMEOUT")
        return {"table": None, "status": "VISION_TIMEOUT", "reason": "crop", "seconds": elapsed}
    budget.consume(page)
    original_timeout = getattr(vision, "timeout", None)
    try:
        if original_timeout is not None:
            vision.timeout = min(float(original_timeout), float(timeout_seconds))
        table, diagnostics = vision.transcribe_table(cropped, file_id=file_id, page=page)
    except Exception as exc:                              # timeout or transport failure
        budget.skip(page, type(exc).__name__)
        return {"table": None, "status": "VISION_FAILED", "reason": type(exc).__name__,
                "seconds": perf_counter() - started}
    finally:
        if original_timeout is not None:
            vision.timeout = original_timeout
    return {"table": table, "status": diagnostics.get("status"),
            "reasons": diagnostics.get("reasons", []),
            "seconds": round(perf_counter() - started, 1)}
