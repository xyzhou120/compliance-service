"""Validation and normalization for the upstream task wire contract.

This module deliberately has no HTTP or database side effects.  The API layer
can use the normalized record to persist a task and to enqueue one run per
item; the callback URL in the task is kept as a snapshot for that execution.
"""

from __future__ import annotations

import copy
from urllib.parse import urlsplit
from typing import Any, Iterable

from .contracts import DEFINITIONS, InvalidRequest, digest, required_text, resolve_check_cycle


# The supplied wire example only defines ``running``.  Pause/resume strings are
# deliberately opt-in through ``status_map`` until the upstream contract is
# confirmed; accepting invented status values would create an unsafe control
# surface.
DEFAULT_STATUSES = frozenset({"running"})
CONTROL_STATUSES = frozenset({"running", "pause", "resume"})
_FIELDS = frozenset({
    "mineId", "status", "checkCycle", "items", "resultCallbackUrl",
    "taskId", "executionId",
})
_ITEM_FIELDS = frozenset({"itemCode", "itemName"})
_ITEM_NAMES = {
    "stop_work_unload": "冲击危险停工撤人与卸压",
    "relief_effects": "冲击地压解危效果检验",
}


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise InvalidRequest("resultCallbackUrl must be a non-empty URL of at most 2048 characters")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise InvalidRequest("resultCallbackUrl must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise InvalidRequest("resultCallbackUrl is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise InvalidRequest("resultCallbackUrl must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise InvalidRequest("resultCallbackUrl must contain a host and no credentials")
    if parsed.fragment:
        raise InvalidRequest("resultCallbackUrl must not contain a fragment")
    # urlsplit may accept malformed ports lazily; force validation here.
    try:
        parsed.port
    except ValueError as exc:
        raise InvalidRequest("resultCallbackUrl has an invalid port") from exc
    return value


def _items(value: Any, *, inspection_config: dict | None = None, identity_only: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise InvalidRequest("items must contain between 1 and 100 entries")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - _ITEM_FIELDS:
            raise InvalidRequest("invalid task item fields")
        code = required_text(item.get("itemCode"), "items.itemCode")
        configured = None
        if inspection_config is not None:
            from .inspection_config import find_item
            configured = find_item(inspection_config, code)
        if not identity_only and code not in DEFINITIONS and configured is None:
            raise InvalidRequest(f"unknown itemCode: {code}")
        if code in seen:
            raise InvalidRequest("duplicate itemCode in one task")
        seen.add(code)
        expected_name = (configured or {}).get("name") or _ITEM_NAMES.get(code, "")
        name = item.get("itemName")
        if name is not None:
            name = required_text(name, "items.itemName")
            if not identity_only and expected_name and name not in {expected_name, *((configured or {}).get("name_aliases") or [])}:
                raise InvalidRequest(f"itemName does not match itemCode: {code}")
        result.append({"item_code": code, **({"item_name": name} if name is not None else {})})
    return result


def _stable_key(payload: dict[str, Any], *, task_id: str | None, execution_id: str | None) -> str:
    if execution_id:
        return f"execution:{digest({'taskId': task_id, 'executionId': execution_id})}"
    if task_id:
        return f"task:{task_id}"
    # Status and callback URL are intentionally excluded: a repeated control
    # request addresses the same execution. A changed callback must be
    # rejected by the persistence layer as a task snapshot conflict.
    identity = {
        "mineId": payload["mineId"],
        "checkCycle": payload["checkCycle"],
        "items": sorted(payload["items"]),
    }
    return f"request:{digest(identity)}"


def normalize_upstream_task(
    payload: Any,
    *,
    status_map: dict[str, str] | None = None,
    inspection_config: dict | None = None,
    identity_only: bool = False,
) -> dict[str, Any]:
    """Validate one upstream task and return a stable internal snapshot.

    The returned ``raw_payload`` is a deep copy, so callers can safely retain
    the exact request received while normalizing other fields.  No task is
    created and no callback is sent by this function.
    """
    if not isinstance(payload, dict):
        raise InvalidRequest("upstream task must be an object")
    if set(payload) - _FIELDS:
        raise InvalidRequest("unknown upstream task fields")
    mine_id = required_text(payload.get("mineId"), "mineId")
    cycle = required_text(resolve_check_cycle(payload.get("checkCycle")), "checkCycle")
    status = required_text(payload.get("status"), "status")
    status_map = dict(status_map or {})
    statuses = frozenset({"running", *status_map})
    if status not in statuses:
        raise InvalidRequest(f"unsupported upstream task status: {status}")
    items = _items(payload.get("items"), inspection_config=inspection_config, identity_only=identity_only)
    callback_url = _url(payload.get("resultCallbackUrl"))
    task_id = payload.get("taskId")
    execution_id = payload.get("executionId")
    if task_id is not None:
        task_id = required_text(task_id, "taskId")
    if execution_id is not None:
        execution_id = required_text(execution_id, "executionId")
    action = status_map.get(status, status)
    if action not in CONTROL_STATUSES:
        raise InvalidRequest(f"unsupported normalized control action: {action}")
    normalized = {
        "mine_id": mine_id,
        "mine_code": mine_id,
        "status": status,
        "action": action,
        "check_cycle": cycle,
        "items": items,
        "result_callback_url": callback_url,
        "callback_url": callback_url,
        "task_id": task_id,
        "execution_id": execution_id,
    }
    normalized["task_key"] = _stable_key(
        {"mineId": mine_id, "checkCycle": cycle,
         "items": [item["item_code"] for item in items]},
        task_id=task_id,
        execution_id=execution_id,
    )
    normalized["raw_payload"] = copy.deepcopy(payload)
    normalized["raw"] = copy.deepcopy(payload)
    return normalized


def validate_control_status(status: Any, *, allowed_statuses: Iterable[str] = CONTROL_STATUSES) -> str:
    """Validate a control status without implying pause/resume completion."""
    value = required_text(status, "status")
    if value not in frozenset(allowed_statuses):
        raise InvalidRequest(f"unsupported control status: {value}")
    return value


def control_response() -> dict[str, Any]:
    """The synchronous response contract for a task control request."""
    return {"code": 200, "success": True, "msg": "任务控制请求已生效"}

