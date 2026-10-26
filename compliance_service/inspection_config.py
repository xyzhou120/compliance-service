"""Configurable inspection item and OceanBase material registry.

The registry is intentionally data-only.  Rule handlers remain code, while
item codes, material identifiers and period policy can change per environment.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any

from .contracts import Conflict, InvalidRequest


DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "inspection_defaults.json"
_GROUPS = frozenset({
    "prevention_daily", "dispatch_daily", "analysis", "release",
    "relief_acceptance", "threshold", "design", "workface_risk", "coal_risk",
    "risk_assessment", "monitoring", "effect_test", "production",
})
_ROLES = frozenset({"actual_record", "requirement", "statement", "regulation"})
_CADENCES = frozenset({"annual", "semiannual", "monthly", "daily", "weekly"})
_HANDLERS = frozenset({"stop_work_unload", "relief_effects"})
_MONTH_SOURCES = frozenset({"current_month", "task_month", "fixed"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def revision(document: dict) -> str:
    import hashlib
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise InvalidRequest(f"{field} must be a non-empty string of at most {max_length} characters")
    return value.strip()


def _month(value: Any, field: str) -> str:
    if type(value) is int:
        value = f"{value:02d}"
    if not isinstance(value, str) or not re.fullmatch(r"(?:0[1-9]|1[0-2])", value):
        raise InvalidRequest(f"{field} must be a two-digit month")
    return value


def default_config() -> dict:
    """Return a fresh copy of the packaged environment-independent defaults."""
    try:
        return validate_config(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidRequest("packaged inspection defaults could not be read") from exc


def validate_config(document: Any) -> dict:
    if not isinstance(document, dict):
        raise InvalidRequest("inspection config must be an object")
    if set(document) != {"schema_version", "query_policy", "items"}:
        raise InvalidRequest("inspection config has unknown fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise InvalidRequest("unsupported inspection config schema_version")
    policy = document["query_policy"]
    if not isinstance(policy, dict) or set(policy) - {"month_source", "fixed_month"} or "month_source" not in policy:
        raise InvalidRequest("query_policy requires month_source and optional fixed_month")
    if not isinstance(policy["month_source"], str) or policy["month_source"] not in _MONTH_SOURCES:
        raise InvalidRequest("unsupported inspection query month source")
    if policy["month_source"] == "fixed":
        _month(policy.get("fixed_month"), "query_policy.fixed_month")
    elif policy.get("fixed_month") is not None:
        raise InvalidRequest("fixed_month is only allowed with month_source=fixed")
    items = document["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise InvalidRequest("inspection config requires between 1 and 100 items")
    try:
        if len(_canonical(document).encode("utf-8")) > 1024 * 1024:
            raise InvalidRequest("inspection config exceeds one MiB")
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("inspection config must contain valid JSON data") from exc
    result = copy.deepcopy(document)
    if result["query_policy"]["month_source"] == "fixed":
        result["query_policy"]["fixed_month"] = _month(result["query_policy"].get("fixed_month"), "query_policy.fixed_month")
    codes = set()
    for index, item in enumerate(result["items"]):
        prefix = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != {"itemCode", "name", "handler", "enabled", "name_aliases", "materials"}:
            raise InvalidRequest(f"{prefix} has unknown or missing fields")
        code = _text(item["itemCode"], f"{prefix}.itemCode", max_length=96)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", code):
            raise InvalidRequest(f"{prefix}.itemCode has invalid characters")
        if code in codes:
            raise InvalidRequest(f"duplicate itemCode: {code}")
        codes.add(code)
        item["itemCode"] = code
        item["name"] = _text(item["name"], f"{prefix}.name")
        if not isinstance(item["handler"], str) or item["handler"] not in _HANDLERS:
            raise InvalidRequest(f"unsupported handler: {item['handler']}")
        if type(item["enabled"]) is not bool:
            raise InvalidRequest(f"{prefix}.enabled must be boolean")
        aliases = item["name_aliases"]
        if not isinstance(aliases, list) or len(aliases) > 20 or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in aliases):
            raise InvalidRequest(f"{prefix}.name_aliases must be a list of short strings")
        item["name_aliases"] = list(dict.fromkeys(v.strip() for v in aliases))
        groups, ids = set(), set()
        if not isinstance(item["materials"], list) or not 1 <= len(item["materials"]) <= 100:
            raise InvalidRequest(f"{prefix}.materials must contain between 1 and 100 entries")
        for mindex, material in enumerate(item["materials"]):
            mp = f"{prefix}.materials[{mindex}]"
            if not isinstance(material, dict) or set(material) != {"material_group", "name", "file_name_id", "role", "cadence"}:
                raise InvalidRequest(f"{mp} has unknown or missing fields")
            group = _text(material["material_group"], f"{mp}.material_group", max_length=64)
            if group not in _GROUPS or group in groups:
                raise InvalidRequest(f"unsupported or duplicate material_group: {group}")
            groups.add(group)
            material["material_group"] = group
            material["name"] = _text(material["name"], f"{mp}.name")
            file_id = _text(material["file_name_id"], f"{mp}.file_name_id", max_length=256)
            if file_id in ids:
                raise InvalidRequest(f"duplicate file_name_id in {code}: {file_id}")
            ids.add(file_id)
            material["file_name_id"] = file_id
            if (not isinstance(material["role"], str) or material["role"] not in _ROLES
                    or not isinstance(material["cadence"], str) or material["cadence"] not in _CADENCES):
                raise InvalidRequest(f"unsupported role or cadence in {mp}")
    return result


def load_config(path: str | Path | None = None) -> dict:
    target = Path(path or os.environ.get("COMPLIANCE_INSPECTION_CONFIG") or DEFAULT_PATH)
    if not target.exists():
        return default_config()
    try:
        if target.stat().st_size > 1024 * 1024:
            raise InvalidRequest("inspection config exceeds one MiB")
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidRequest("inspection config could not be read") from exc
    return validate_config(document)


def find_item(config: dict | None, code: str) -> dict:
    document = validate_config(default_config() if config is None else config)
    for item in document["items"]:
        if item["itemCode"] == code:
            if not item["enabled"]:
                raise InvalidRequest(f"inspection item is disabled: {code}")
            return copy.deepcopy(item)
    raise InvalidRequest(f"unknown itemCode: {code}")


def public_config(config: dict | None = None) -> dict:
    document = validate_config(config or load_config())
    return copy.deepcopy(document)


@contextmanager
def _file_lock(path: Path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class InspectionConfigRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("COMPLIANCE_INSPECTION_CONFIG") or DEFAULT_PATH)

    def read(self) -> dict:
        config = load_config(self.path)
        return {"config": config, "revision": revision(config), "path": str(self.path)}

    def get_item(self, code: str, config: dict | None = None) -> dict:
        return find_item(config if config is not None else self.read()["config"], code)

    def save(self, document: Any, expected_revision: str | None = None) -> dict:
        validated = validate_config(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.path):
            current = load_config(self.path)
            current_revision = revision(current)
            if expected_revision is not None and expected_revision != current_revision:
                raise Conflict("inspection config changed; refresh before saving")
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            payload = (_canonical(validated) + "\n").encode("utf-8")
            try:
                with temporary.open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.read()


Registry = InspectionConfigRegistry

