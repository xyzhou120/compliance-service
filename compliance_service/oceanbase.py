"""Read-only OceanBase catalog adapter and immutable query snapshots.

The adapter deliberately depends on an injected DB-API connection factory.  It
does not create credentials, open a connection at import time, or permit caller
selected file IDs/URLs.  The real tenant schema must be confirmed during
联调; SQL and column names are kept in one small adapter for that reason.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from .contracts import InvalidRequest, canonical, digest, resolve_check_cycle

def _default_catalog():
    from .inspection_config import default_config
    return {item["itemCode"]: {material["material_group"]: dict(material) for material in item["materials"]}
            for item in default_config()["items"]}


# Compatibility export for callers inspecting the packaged defaults. Queries
# resolve the active registry or an accepted immutable per-run configuration.
CATALOG = _default_catalog()

# Static identifiers are intentionally not configurable through requests.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
QUERY_VERSION = "oceanbase-catalog-v3-object-address"

def _period(check_cycle: str) -> tuple[str, str]:
    if not isinstance(check_cycle, str) or not re.fullmatch(r"\d{4}-\d{2}", check_cycle):
        raise InvalidRequest("checkCycle must use YYYY-MM")
    year, month = map(int, check_cycle.split("-"))
    if not 1 <= month <= 12:
        raise InvalidRequest("checkCycle month is invalid")
    try:
        start = date(year, month, 1)
        end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    except ValueError as exc:
        raise InvalidRequest("checkCycle date is invalid") from exc
    return start.isoformat(), end.isoformat()

def _rows(cursor) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    desc = getattr(cursor, "description", None) or ()
    names = [d[0] if isinstance(d, (tuple, list)) else str(d) for d in desc]
    result = []
    for value in values:
        if isinstance(value, Mapping):
            result.append(dict(value))
        else:
            result.append(dict(zip(names, value)))
    return result

def _pick(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None

def _attachment_url(row: Mapping[str, Any], base_url: str | None = None) -> Any:
    explicit = _pick(row, "attachment_url", "attachment_link", "url")
    if explicit:
        return explicit
    # Real catalogs may store either full URLs or object keys in these fields.
    # Object keys need a deployment-supplied endpoint; never invent its origin.
    for name in ("local_attachment_address", "attachment_address", "minio_address"):
        value = row.get(name)
        if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
            return value
    bucket, key = row.get("local_bucket_name"), row.get("local_attachment_address")
    if base_url and isinstance(bucket, str) and bucket and isinstance(key, str) and key:
        if "/" in bucket or "\\" in bucket or bucket in {".", ".."} or "\\" in key or any(part in {".", ".."} for part in key.split("/")):
            raise InvalidRequest("invalid attachment object path")
        return base_url.rstrip("/") + "/" + quote(bucket, safe="") + "/" + quote(key.lstrip("/"), safe="/")
    return None

class OceanBaseRepository:
    """Query the server-owned attachment catalog in read-only mode."""
    def __init__(self, connection_factory: Callable[[], Any], *, table: str = "mine_remote_disaster_file", compatibility_mode: str = "mysql", query_version: str = QUERY_VERSION, source_system: str = "oceanbase", environment: str = "test", inspection_config: dict | None = None, config_path: str | None = None, month_source: str | None = None, fixed_month: str | None = None, attachment_base_url: str | None = None):
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        if compatibility_mode != "mysql":
            raise InvalidRequest("OceanBase adapter requires explicit mysql compatibility mode")
        if not _TABLE_RE.fullmatch(table):
            raise InvalidRequest("invalid catalog table identifier")
        self.connection_factory = connection_factory
        self.table = table
        self.compatibility_mode = compatibility_mode
        self.query_version = query_version
        self.source_system = source_system
        self.environment = environment
        self.config_path = config_path
        self.month_source = month_source
        self.fixed_month = fixed_month
        self.inspection_config = inspection_config
        if attachment_base_url is not None:
            if not isinstance(attachment_base_url, str):
                raise InvalidRequest("attachment base URL must be a string")
            parts = urlsplit(attachment_base_url)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise InvalidRequest("invalid attachment base URL")
        self.attachment_base_url = attachment_base_url

    def _config(self, override=None):
        from .inspection_config import Registry, default_config, validate_config
        document = override if override is not None else self.inspection_config
        if isinstance(document, dict) and "revision" in document:
            document = {key: value for key, value in document.items() if key not in {"revision", "resolved_query_month"}}
        if document is None:
            document = Registry(self.config_path).read()["config"] if self.config_path else default_config()
        if isinstance(document, dict) and isinstance(document.get("config"), dict):
            document = document["config"]
        if isinstance(document, dict) and "itemCode" in document and "items" not in document:
            fields = ("itemCode", "name", "handler", "enabled", "name_aliases", "materials")
            document = {"schema_version": 1, "query_policy": document.get("query_policy", {"month_source": self.month_source or "task_month"}),
                        "items": [{key: document[key] for key in fields}]}
        return validate_config(document)

    @staticmethod
    def _month(check_cycle, source, fixed):
        # Policy flags are accepted for compatibility with older callers, but
        # cannot override an explicitly supplied cycle on a new query.
        return check_cycle[5:7]

    def _item(self, config, item_code):
        from .inspection_config import find_item
        return find_item(config, item_code)

    def query(self, mine_code: str, check_cycle: str | None, item_code: str, *, inspection_config: dict | None = None, month_source: str | None = None, fixed_month: str | None = None, resolved_month: str | None = None) -> dict[str, Any]:
        if not isinstance(mine_code, str) or not mine_code.strip():
            raise InvalidRequest("mineId is required")
        check_cycle = resolve_check_cycle(check_cycle)
        start, end = _period(check_cycle)
        config = self._config(inspection_config)
        config_revision = (inspection_config or {}).get("revision") or digest(config)
        item = self._item(config, item_code)
        materials = item.get("materials", [])
        if not materials:
            raise InvalidRequest("inspection item has no configured materials")
        entries = {m["material_group"]: m for m in materials}
        ids = [m["file_name_id"] for m in materials]
        by_cadence = {}
        for material in materials:
            by_cadence.setdefault(material.get("cadence", "monthly"), []).append(material["file_name_id"])
        accepted_month = (inspection_config or {}).get("resolved_query_month")
        if accepted_month:
            # These values were frozen when an earlier task was admitted.
            # Changing today's policy must not widen or move that task's scope.
            source = (inspection_config or {}).get("query_policy", {}).get("month_source", "task_month")
            fixed = (inspection_config or {}).get("query_policy", {}).get("fixed_month")
            if resolved_month is not None and resolved_month != accepted_month:
                raise InvalidRequest("resolved query month differs from accepted task scope")
            month = accepted_month
        else:
            source, fixed = "task_month", None
            month = check_cycle[5:7]
            if resolved_month is not None and resolved_month != month:
                raise InvalidRequest("resolved query month requires an accepted frozen task scope")
        if not isinstance(month, str) or not re.fullmatch(r"(?:0[1-9]|1[0-2])", month):
            raise InvalidRequest("resolved query month must use MM")
        year = int(check_cycle[:4])
        start, end = _period(f"{year:04d}-{month}")
        marks = ", ".join(["%s"] * len(ids))
        predicates, params = [], [mine_code, *ids]
        for cadence, cadence_ids in by_cadence.items():
            cm = ", ".join(["%s"] * len(cadence_ids))
            if cadence == "annual":
                predicates.append(f"(file_name_id IN ({cm}) AND report_time_year = %s)")
                params.extend(cadence_ids); params.append(year)
            elif cadence == "semiannual":
                predicates.append(f"(file_name_id IN ({cm}) AND report_time_year = %s AND report_time_month IN (%s, %s))")
                params.extend(cadence_ids); params.extend([year, "01", "07"])
            else:
                predicates.append(f"(file_name_id IN ({cm}) AND report_time_year = %s AND report_time_month = %s)")
                params.extend(cadence_ids); params.extend([year, month])
        sql = (f"SELECT * FROM {self.table} WHERE mine_code = %s AND file_name_id IN ({marks}) "
               f"AND ({' OR '.join(predicates)}) ORDER BY report_time_year, attachment_id")
        started = datetime.now(timezone.utc).isoformat()
        connection = self.connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            raw_rows = _rows(cursor)
        finally:
            try: cursor.close()
            except Exception: pass
            try: connection.close()
            except Exception: pass
        finished = datetime.now(timezone.utc).isoformat()
        normalized = []
        raw_snapshot_rows = []
        seen: dict[str, dict[str, Any]] = {}
        conflicts: dict[tuple[str, str], set[str]] = {}
        for index, row in enumerate(raw_rows, 1):
            raw_snapshot_rows.append({str(k): (v.isoformat() if hasattr(v, "isoformat") else v if isinstance(v, (str, int, float, bool)) or v is None else str(v)) for k, v in row.items()})
            returned_mine = _pick(row, "mine_code", "mineCode", "mine_id", "mineId")
            if returned_mine is not None and str(returned_mine) != mine_code:
                raise InvalidRequest("catalog row belongs to another mine")
            aid = _pick(row, "attachment_id", "attachmentId", "id")
            if aid is None or not str(aid).strip():
                raise InvalidRequest("catalog row has no stable attachment_id")
            aid = str(aid)
            fid = str(_pick(row, "file_name_id", "fileNameId") or "")
            meta = next((m for m in entries.values() if m["file_name_id"] == fid), None)
            if meta is None:
                raise InvalidRequest("catalog returned a file_name_id outside the server catalog")
            returned_year = _pick(row, "report_time_year", "reportTimeYear")
            returned_month = _pick(row, "report_time_month", "reportTimeMonth")
            if returned_year is not None and str(returned_year) != str(year):
                raise InvalidRequest("catalog row belongs to another report year")
            if returned_month is not None and meta["cadence"] != "annual":
                allowed_months = {"01", "07"} if meta["cadence"] == "semiannual" else {month}
                if str(returned_month).zfill(2) not in allowed_months:
                    raise InvalidRequest("catalog row belongs to another query month")
            identity = {"file_name_id": fid, "attachment_name": str(_pick(row, "attachment_name", "file_name", "name") or aid), "attachment_url": _attachment_url(row, self.attachment_base_url), "report_date": str(_pick(row, "report_date", "reportDate", "business_date", "report_time") or ""), "report_time_year": returned_year, "report_time_month": returned_month,
                        "object_location": {key: row.get(key) for key in ("bucket_name", "attachment_address", "local_bucket_name", "local_attachment_address", "minio_address") if row.get(key) is not None}}
            if aid in seen:
                if seen[aid] != identity:
                    raise InvalidRequest("duplicate attachment_id has conflicting metadata")
                continue
            seen[aid] = identity
            report_date = _pick(row, "report_date", "reportDate", "business_date", "report_time")
            report_date = report_date.isoformat() if hasattr(report_date, "isoformat") else (str(report_date) if report_date is not None else None)
            record = {
                "attachment_id": aid,
                "file_name_id": fid,
                "material_group": next(group for group, item in entries.items() if item is meta),
                "role": meta["role"], "cadence": meta["cadence"],
                "attachment_name": str(_pick(row, "attachment_name", "file_name", "name") or aid),
                "attachment_url": _attachment_url(row, self.attachment_base_url),
                "report_date": report_date,
                "report_time_year": _pick(row, "report_time_year", "reportTimeYear"),
                "report_time_month": _pick(row, "report_time_month", "reportTimeMonth"),
                "source_row": index,
                "source_system": "oceanbase",
            }
            normalized.append(record)
            key = (record["material_group"], report_date or "")
            conflicts.setdefault(key, set()).add(aid)
        conflict_rows = [{"material_group": group, "report_date": day, "attachment_ids": sorted(ids)}
                         for (group, day), ids in conflicts.items() if len(ids) > 1]
        snapshot = {
            "schema_version": "oceanbase-catalog-snapshot-v1",
            "profile": "oceanbase-catalog", "source_system": self.source_system, "environment": self.environment,
            "mine_code": mine_code, "item_code": item_code,
            "inspection_config_revision": config_revision,
            "check_cycle": check_cycle, "period": {"start": start, "end": (date.fromisoformat(end).fromordinal(date.fromisoformat(end).toordinal()-1)).isoformat()},
            "catalog": {"version": self.query_version, "table": self.table, "compatibility_mode": self.compatibility_mode, "item": item, "inspection_config_revision": config_revision, "entries": entries},
            "query": {"sql": sql, "params": list(params), "started_at": started, "finished_at": finished, "query_version": self.query_version,
                      "query_policy": {"month_source": source, "fixed_month": fixed, "resolved_month": month, "report_time_year": year},
                      "selected_predicates": {"annual": by_cadence.get("annual", []), "semiannual": by_cadence.get("semiannual", []), "month_filtered": [m for c, values in by_cadence.items() if c != "annual" and c != "semiannual" for m in values]}},
            "raw_rows": raw_snapshot_rows, "rows": normalized, "conflicts": conflict_rows, "source_complete": False,
            "coverage": {"catalog_index_only": True, "row_count": len(normalized)},
        }
        snapshot["snapshot_id"] = digest(snapshot)
        return snapshot

__all__ = ["CATALOG", "OceanBaseRepository"]
