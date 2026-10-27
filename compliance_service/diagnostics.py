"""Gap and rejection diagnostics for the operation dashboard."""

from __future__ import annotations

REASON_BUCKETS = {
    "LOW_DPI": "采集质量", "SKEW": "采集质量", "QUALITY_TIER_POOR": "采集质量",
    "OCR_CONF_BELOW": "识别不确定", "CHANNEL_DISAGREE": "识别不确定",
    "VL_UNDERREAD": "识别不确定", "EXPONENT_LOST_OCR": "识别不确定",
    "NO_TEXT_AND_NO_PAINTED_IMAGE": "内容缺失", "UNREADABLE_PAGE": "内容缺失",
    "UNSUPPORTED_FORMAT": "格式不支持", "UNKNOWN_TABLE": "版式未识别",
}


def reason_summary(snapshot: dict) -> dict:
    """Aggregate reason codes per bucket and per file from a run snapshot."""
    per_file = {}
    per_bucket = {}
    for receipt in snapshot.get("page_receipts") or []:
        if not isinstance(receipt, dict):
            continue
        file_id = receipt.get("file_id") or "unknown"
        for code in receipt.get("reason_codes") or []:
            per_file.setdefault(file_id, {}).setdefault(code, 0)
            per_file[file_id][code] += 1
            bucket = REASON_BUCKETS.get(code, "其他")
            per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
    total_pages = len(snapshot.get("page_receipts") or [])
    blocked = sum(1 for r in snapshot.get("page_receipts") or []
                  if isinstance(r, dict) and r.get("status") in {"unreadable", "rejected_by_quality"})
    return {"pages": total_pages, "blocked_pages": blocked,
            "blocked_rate": round(blocked / total_pages, 4) if total_pages else 0.0,
            "buckets": dict(sorted(per_bucket.items())), "per_file": per_file}


def priorities(summary: dict) -> list:
    """Order buckets by volume so operations know what to fix first."""
    return [bucket for bucket, _ in sorted(summary.get("buckets", {}).items(),
                                           key=lambda item: (-item[1], item[0]))]
