"""Chart handling: a chart may only carry numbers when it is verifiable.

Trend charts in real reports usually have no data labels, so they are treated as
semantic context (kind, title, axes, covered dates) and never as a numeric
source. The four checks below are the only path to a chart-derived number.
"""

from __future__ import annotations

TOLERANCE = 0.02


def _ticks(payload) -> list:
    values = []
    for item in payload or []:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            return []
    return values


def _monotonic(values) -> bool:
    return all(right > left for left, right in zip(values, values[1:]))


def _linear(values) -> bool:
    if len(values) < 3:
        return len(values) == 2
    gaps = [right - left for left, right in zip(values, values[1:])]
    base = sum(gaps) / len(gaps)
    if base <= 0:
        return False
    return all(abs(gap - base) <= base * TOLERANCE for gap in gaps)


def validate_chart(payload: dict) -> dict:
    """Four-check gate for chart-derived numbers."""
    reasons = []
    if not isinstance(payload, dict):
        return {"status": "chart_rejected", "reasons": ["NO_PAYLOAD"]}
    y_ticks = _ticks((payload.get("y_axis") or {}).get("ticks"))
    x_ticks = (payload.get("x_axis") or {}).get("ticks") or []
    if not y_ticks or not _monotonic(y_ticks):
        reasons.append("Y_AXIS_NOT_MONOTONIC")
    elif not _linear(y_ticks):
        reasons.append("Y_AXIS_NOT_LINEAR")
    if not payload.get("data_labels_present"):
        reasons.append("NO_DATA_LABELS")
    series = payload.get("series") or []
    values = []
    for item in series:
        if not isinstance(item, dict):
            continue
        for value in item.get("values") or []:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                reasons.append("NON_NUMERIC_VALUE")
    if values and y_ticks and (min(values) < min(y_ticks) or max(values) > max(y_ticks)):
        reasons.append("VALUE_OUTSIDE_AXIS_RANGE")
    if values and x_ticks and len(values) > len(x_ticks) * len(series or [1]):
        reasons.append("VALUE_COUNT_EXCEEDS_CATEGORIES")
    status = "chart_verified" if not reasons else (
        "chart_semantic_only" if "NO_DATA_LABELS" in reasons else "chart_rejected")
    return {"status": status, "reasons": reasons,
            "chart_type": payload.get("chart_type"), "title": payload.get("title"),
            "x_ticks": list(x_ticks), "y_ticks": y_ticks,
            "values": values, "confidence": payload.get("confidence")}


def read_chart(vlm_callable, image: bytes, *, prompt: str | None = None) -> dict:
    """Ask the vision model for a JSON chart description and apply the gate."""
    question = prompt or (
        '只输出 JSON：{"chart_type":"bar|line|area|scatter|unknown","title":"...",'
        '"x_axis":{"label":"...","ticks":[...]},"y_axis":{"label":"...","unit":"...","ticks":[...]},'
        '"series":[{"name":"...","values":[...]}],"data_labels_present":true|false,"confidence":0-1}.'
        "只有图上明确标注的数字才填 values；读不出就留空数组，绝不估算。")
    payload = vlm_callable(image, question)
    return validate_chart(payload if isinstance(payload, dict) else {})


def chart_semantics(payload: dict) -> dict:
    """What a chart is allowed to contribute when its numbers are not verified."""
    return {"chart_type": payload.get("chart_type"),
            "title": payload.get("title"),
            "x_axis_label": (payload.get("x_axis") or {}).get("label"),
            "y_axis_label": (payload.get("y_axis") or {}).get("label"),
            "unit": (payload.get("y_axis") or {}).get("unit"),
            "categories": list((payload.get("x_axis") or {}).get("ticks") or []),
            "series_names": [item.get("name") for item in payload.get("series") or []
                             if isinstance(item, dict)]}
