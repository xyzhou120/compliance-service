"""Small, read-only projections for the local debugging frontend.

The ledger remains authoritative.  These views select JSON fields in SQLite so
polling a task or opening a report does not deserialize document snapshots or
send every evidence block to the browser.  Evidence and diagnostics are paged.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3

from .contracts import InvalidRequest, NotFound


_LOCAL_SQL = ("json_extract(spec_json,'$.environment') IN ('local','test') "
              "AND json_extract(spec_json,'$.callback_mode') IN ('none','mock')")
_TERMINAL = {"completed", "failed"}
_EVIDENCE_FILE = """coalesce(json_extract(e.value,'$.file_id'),json_extract(e.value,'$.source_file_id'),
    (SELECT json_extract(f.value,'$.file_id') FROM json_each(snapshot_json,'$.files') AS f
     WHERE json_extract(f.value,'$.content_hash')=json_extract(e.value,'$.content_hash') LIMIT 1))"""
_FILE_COUNT = """coalesce(json_array_length(snapshot_json,'$.files'),0)+
    (SELECT count(*) FROM json_each(coalesce(json_extract(snapshot_json,'$.acquisition.rows'),
       json_extract(acquisition_json,'$.rows'),'[]')) AS q WHERE NOT EXISTS
       (SELECT 1 FROM json_each(snapshot_json,'$.files') AS f
        WHERE json_extract(f.value,'$.file_id')=json_extract(q.value,'$.attachment_id')))"""


def _json(value, default=None):
    return json.loads(value) if value is not None else default


def _page(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise InvalidRequest("offset must be non-negative and limit must be between 1 and 100")


def _response(items, total, offset, limit):
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(items) < total}


def _query_scope(row):
    """Show the frozen query, including older tasks whose month differs."""
    policy = _json(row["actual_query_policy"], {}) or _json(row["query_policy"], {})
    period = _json(row["query_period"])
    month = policy.get("resolved_month") or row["resolved_query_month"]
    requested = row["check_cycle"]
    resolved = str(period.get("start", ""))[:7] if period else None
    if not resolved and month and isinstance(requested, str) and len(requested) >= 7:
        resolved = requested[:4] + "-" + str(month).zfill(2)
        try:
            year, month_number = map(int, resolved.split("-"))
            start = date(year, month_number, 1)
            following = date(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
            period = {"start": start.isoformat(), "end": (following - timedelta(days=1)).isoformat()}
        except (ValueError, OverflowError):
            resolved, period = None, None
    return {"requested_cycle": requested, "resolved_cycle": resolved,
            "period": period, "month_source": policy.get("month_source"),
            "source": "frozen_query" if row["query_period"] else "accepted_task",
            "differs_from_task": bool(resolved and resolved != requested)}


class DebugViews:
    def __init__(self, service):
        self.service = service

    def _local(self):
        if getattr(self.service, "task_token", None) or getattr(self.service, "environment", None) == "production":
            raise NotFound("local debugging views are unavailable in production")

    @contextmanager
    def _read(self):
        self._local()
        uri = Path(self.service.ledger.path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _owner(self, db, run_id):
        row = db.execute(
            "SELECT run_id,json_extract(spec_json,'$.source_system') AS source_system,"
            "json_extract(spec_json,'$.mine_code') AS mine_code,"
            "json_extract(spec_json,'$.environment') AS environment FROM runs WHERE run_id=? AND " + _LOCAL_SQL,
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFound("run not found")
        return row

    def _progress_summary(self, db, run_id, latest, catalog_files):
        # Aggregate in SQLite; never transfer all events or document snapshots
        # just to refresh the compact progress strip.
        counts = db.execute("""WITH events AS (
                SELECT sequence,json_extract(event_json,'$.stage') AS stage,
                    json_extract(event_json,'$.event') AS event,
                    json_extract(event_json,'$.file_id') AS file_id,
                    json_extract(event_json,'$.page') AS page,
                    json_extract(event_json,'$.status') AS status
                FROM run_progress WHERE run_id=?),
            downloads AS (SELECT max(sequence) AS sequence FROM events
                WHERE stage='acquisition' AND event='attachment_finished' GROUP BY file_id),
            processed AS (SELECT max(sequence) AS sequence FROM events
                WHERE stage='source' AND event='file_finished' GROUP BY file_id),
            pages AS (SELECT max(sequence) AS sequence FROM events
                WHERE stage='recognition' AND event='page_finished' GROUP BY file_id,page)
            SELECT
              (SELECT count(*) FROM events JOIN downloads USING(sequence) WHERE status='completed') AS downloaded,
              (SELECT count(*) FROM events JOIN downloads USING(sequence) WHERE status='failed') AS download_failed,
              (SELECT count(*) FROM processed) AS processed_files,
              (SELECT count(*) FROM events JOIN processed USING(sequence) WHERE status='failed') AS processing_failed,
              (SELECT count(*) FROM pages) AS recognition_pages_completed,
              (SELECT count(*) FROM events JOIN pages USING(sequence) WHERE status='failed') AS recognition_pages_failed,
              (SELECT max(json_extract(event_json,'$.file_total')) FROM run_progress WHERE run_id=?
                 AND json_extract(event_json,'$.stage')='source') AS processing_files
            """, (run_id, run_id)).fetchone()
        current = db.execute("""SELECT event_json,sequence FROM run_progress AS start
            WHERE run_id=? AND json_extract(event_json,'$.stage')='source'
              AND json_extract(event_json,'$.event')='file_started'
              AND NOT EXISTS (SELECT 1 FROM run_progress AS finished WHERE finished.run_id=start.run_id
                AND finished.sequence>start.sequence AND json_extract(finished.event_json,'$.stage')='source'
                AND json_extract(finished.event_json,'$.event')='file_finished'
                AND json_extract(finished.event_json,'$.file_id')=json_extract(start.event_json,'$.file_id'))
            ORDER BY sequence DESC LIMIT 1""", (run_id,)).fetchone()
        current_file = None
        if current:
            entry = _json(current["event_json"], {})
            page = db.execute("""SELECT json_extract(event_json,'$.page') AS page,
                    json_extract(event_json,'$.page_total') AS page_total
                FROM run_progress WHERE run_id=? AND sequence>=?
                  AND json_extract(event_json,'$.file_id')=? AND json_extract(event_json,'$.page') IS NOT NULL
                ORDER BY sequence DESC LIMIT 1""", (run_id, current["sequence"], entry.get("file_id"))).fetchone()
            total = db.execute("""SELECT json_extract(event_json,'$.page_total') AS page_total
                FROM run_progress WHERE run_id=? AND sequence>=?
                  AND json_extract(event_json,'$.file_id')=? AND json_extract(event_json,'$.page_total') IS NOT NULL
                ORDER BY sequence DESC LIMIT 1""", (run_id, current["sequence"], entry.get("file_id"))).fetchone()
            current_file = {"file_id": entry.get("file_id"), "file_name": entry.get("file_name"),
                            "page": page["page"] if page else None, "page_total": total["page_total"] if total else None}
        warnings = db.execute("SELECT coalesce(json_array_length(snapshot_json,'$.attachment_acquisition.receipt_failures'),0) FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        return {"catalog_files": catalog_files, **dict(counts), "current_file": current_file,
                "storage_warnings": warnings, "stage": latest.get("stage") if latest else None, "latest_event": latest}

    def task(self, record_id):
        with self._read() as db:
            rows = db.execute(
                "SELECT run_id,created,execution_state," + _LOCAL_SQL + " AS local_allowed,"
                "json_extract(spec_json,'$.source_system') AS source_system,"
                "json_extract(spec_json,'$.mine_code') AS mine_code,"
                "json_extract(spec_json,'$.environment') AS environment,"
                "json_extract(spec_json,'$.check_cycle') AS check_cycle,"
                "json_extract(spec_json,'$.item_code') AS item_code,"
                "json_extract(spec_json,'$.inspection_item.name') AS item_name,"
                "json_extract(spec_json,'$.inspection_item.revision') AS inspection_revision,"
                "json_extract(spec_json,'$.inspection_item.query_policy') AS query_policy,"
                "json_extract(spec_json,'$.inspection_item.resolved_query_month') AS resolved_query_month,"
                "coalesce(json_extract(snapshot_json,'$.acquisition.query.query_policy'),json_extract(acquisition_json,'$.query.query_policy')) AS actual_query_policy,"
                "coalesce(json_extract(snapshot_json,'$.acquisition.period'),json_extract(acquisition_json,'$.period')) AS query_period,"
                "json_extract(spec_json,'$.scope.manifest') AS manifest_path,"
                "json_extract(spec_json,'$.scope.fixture') AS fixture,"
                "json_extract(spec_json,'$.scope.oceanbase') AS oceanbase,"
                "json_extract(result_json,'$.status') AS result_status,"
                "json_extract(result_json,'$.description') AS description,"
                "json_extract(snapshot_json,'$.source_complete') AS source_complete," +
                _FILE_COUNT + " AS file_count,"
                "coalesce(json_array_length(snapshot_json,'$.evidence'),0) AS evidence_count,"
                "coalesce((SELECT created FROM run_progress WHERE run_progress.run_id=runs.run_id "
                "AND json_extract(event_json,'$.stage')='run' AND json_extract(event_json,'$.event')='finished' "
                "ORDER BY sequence DESC LIMIT 1),CASE WHEN execution_state IN ('completed','failed') THEN "
                "(SELECT max(created) FROM run_progress WHERE run_progress.run_id=runs.run_id) END) AS finished_at,"
                "(SELECT json_object('sequence',sequence,'created',created,'data',json(event_json)) FROM run_progress "
                "WHERE run_progress.run_id=runs.run_id ORDER BY sequence DESC LIMIT 1) AS latest_event "
                "FROM runs WHERE coalesce(task_key,run_id)=? ORDER BY created,run_id", (record_id,),
            ).fetchall()
            if not rows or any(not row["local_allowed"] for row in rows):
                raise NotFound("task not found")
            first = rows[0]
            owner = (first["source_system"], first["mine_code"], first["environment"])
            if any((r["source_system"], r["mine_code"], r["environment"]) != owner for r in rows):
                raise NotFound("task not found")
            request_row = db.execute(
                "SELECT json_object('mineId',json_extract(request_json,'$.mineId'),"
                "'status',json_extract(request_json,'$.status'),'checkCycle',json_extract(request_json,'$.checkCycle'),"
                "'items',json_extract(request_json,'$.items'),'resultCallbackUrl',json_extract(request_json,'$.resultCallbackUrl'),"
                "'manifestPath',json_extract(request_json,'$.manifestPath'),'dataSource',json_extract(request_json,'$.dataSource'),"
                "'taskId',json_extract(request_json,'$.taskId'),'executionId',json_extract(request_json,'$.executionId')) AS payload "
                "FROM inspection_tasks WHERE task_key=? AND source_system=? AND mine_code=? AND environment=?",
                (record_id, *owner),
            ).fetchone()
            request = _json(request_row["payload"], {}) if request_row else {}
            request = {key: value for key, value in request.items() if value is not None}
            request.setdefault("mineId", first["mine_code"])
            request.setdefault("checkCycle", first["check_cycle"])
            request.setdefault("status", "running")
            request.setdefault("items", [{"itemCode": row["item_code"]} for row in rows])
            if first["manifest_path"]:
                request.setdefault("manifestPath", first["manifest_path"])
                request.setdefault("dataSource", "manifest")
            elif first["oceanbase"]:
                request.setdefault("dataSource", "oceanbase")
            request.update(sourceSystem=first["source_system"], environment=first["environment"])
            now = self.service.ledger.clock()
            items = []
            origins = set()
            for row in rows:
                latest = _json(row["latest_event"])
                if latest:
                    latest = {"sequence": latest["sequence"], "created": latest["created"], **latest["data"]}
                end = row["finished_at"] if row["finished_at"] is not None else now
                items.append({"run_id": row["run_id"], "item_code": row["item_code"],
                    "item_name": row["item_name"] or row["item_code"], "execution_state": row["execution_state"],
                    "result_status": row["result_status"], "description": row["description"],
                    "elapsed_ms": None if row["execution_state"] in _TERMINAL and row["finished_at"] is None else round(max(0, end - row["created"]) * 1000, 2),
                    "file_count": row["file_count"], "evidence_count": row["evidence_count"],
                    "source_complete": None if row["source_complete"] is None else bool(row["source_complete"]),
                    "latest_event": latest, "inspection_revision": row["inspection_revision"],
                    "query_scope": _query_scope(row),
                    "progress": self._progress_summary(db, row["run_id"], latest, row["file_count"]),
                    "created": row["created"], "finished_at": row["finished_at"]})
                origins.add("synthetic" if row["fixture"] or row["source_system"] in {"acceptance-samples", "demo"}
                            or row["mine_code"] in {"SAMPLE-MINE", "synthetic-mine"}
                            else "oceanbase" if row["oceanbase"] else "local_manifest")
            states = {row["execution_state"] for row in rows}
            state = next((s for s in ("running", "queued", "paused", "failed") if s in states), "completed")
            created = min(row["created"] for row in rows)
            finished = max(row["finished_at"] for row in rows) if all(row["finished_at"] is not None for row in rows) else None
            return {"record_id": record_id, "created": created, "source_system": first["source_system"],
                "mine_code": first["mine_code"], "environment": first["environment"], "check_cycle": first["check_cycle"],
                "execution_state": state, "item_count": len(rows),
                "completed_count": sum(row["execution_state"] in _TERMINAL for row in rows),
                "elapsed_ms": None if states <= _TERMINAL and finished is None else round(max(0, (finished if finished is not None else now) - created) * 1000, 2),
                "finished_at": finished, "origin": next(iter(origins)) if len(origins) == 1 else "mixed", "items": items,
                "request": request, "query_policy": _json(first["query_policy"], {})}

    def report(self, run_id):
        with self._read() as db:
            self._owner(db, run_id)
            row = db.execute(
                "SELECT execution_state,json_extract(result_json,'$.status') AS status,"
                "json_extract(result_json,'$.description') AS description,json_extract(result_json,'$.detail') AS detail,"
                "json_extract(result_json,'$.error_type') AS error_type,"
                "json_extract(result_json,'$.error_stage') AS error_stage,"
                "json_extract(result_json,'$.rule_coverage') AS rule_coverage,"
                "json_extract(result_json,'$.rule_baseline') AS rule_baseline,"
                "json_extract(result_json,'$.daily_summary') AS daily_summary,"
                "json_extract(snapshot_json,'$.source_complete') AS source_complete,"
                "json_extract(snapshot_json,'$.observation') AS observation," +
                "json_extract(snapshot_json,'$.acquisition.query.query_policy') AS query_policy," +
                _FILE_COUNT + " AS files,"
                "coalesce(json_array_length(snapshot_json,'$.evidence'),0) AS evidence,"
                "coalesce(json_array_length(result_json,'$.coverage_gaps'),json_array_length(snapshot_json,'$.coverage_gaps'),0) AS gaps,"
                "coalesce(json_array_length(result_json,'$.propositions'),0) AS propositions,"
                "coalesce(json_array_length(snapshot_json,'$.candidates'),0)+coalesce(json_array_length(snapshot_json,'$.daily_candidate_evidence'),0) AS candidates,"
                "coalesce(json_array_length(result_json,'$.findings'),0) AS finding_count,"
                "coalesce(json_array_length(result_json,'$.daily_issues'),0) AS daily_issue_count,"
                "coalesce(json_array_length(result_json,'$.events'),0) AS event_count FROM runs WHERE run_id=?", (run_id,),
            ).fetchone()
            failure_location = None
            if row["status"] == "failed":
                last_failure = db.execute(
                    "SELECT sequence FROM run_progress WHERE run_id=? AND json_extract(event_json,'$.event')='failed' "
                    "ORDER BY sequence DESC LIMIT 1", (run_id,),
                ).fetchone()
                if last_failure:
                    last_page = db.execute(
                        "SELECT json_extract(event_json,'$.file_id') AS file_id,"
                        "json_extract(event_json,'$.page') AS page FROM run_progress WHERE run_id=? AND sequence<? "
                        "AND json_extract(event_json,'$.event') IN ('page_started','file_started') "
                        "ORDER BY sequence DESC LIMIT 1", (run_id, last_failure["sequence"]),
                    ).fetchone()
                    if last_page and last_page["file_id"]:
                        name = db.execute(
                            "SELECT json_extract(value,'$.attachment_name') AS name FROM runs,"
                            "json_each(acquisition_json,'$.rows') WHERE run_id=? "
                            "AND json_extract(value,'$.attachment_id')=? LIMIT 1",
                            (run_id, last_page["file_id"]),
                        ).fetchone()
                        failure_location = {"file_id": last_page["file_id"], "page": last_page["page"],
                                            "file_name": name["name"] if name else None}
            arrays = {}
            for name in ("events", "findings", "daily_issues"):
                values = db.execute(f"SELECT value FROM runs,json_each(result_json,'$.{name}') WHERE run_id=? LIMIT 100", (run_id,)).fetchall()
                arrays[name] = [_json(value["value"]) for value in values]
            event_times = {}
            for fact in db.execute("SELECT json_extract(f.value,'$.event_id') AS event_id,"
                                   "json_extract(f.value,'$.workface') AS workface,json_extract(f.value,'$.time') AS time "
                                   "FROM runs,json_each(snapshot_json,'$.facts') AS f WHERE run_id=? "
                                   "AND json_extract(f.value,'$.kind')='event' LIMIT 1000", (run_id,)):
                if fact["time"]:
                    event_times.setdefault((fact["event_id"], fact["workface"]), set()).add(fact["time"])
            for finding in arrays["findings"]:
                times = event_times.get((finding.get("event_id"), finding.get("workface")), set())
                if len(times) == 1:
                    finding["event_time"] = next(iter(times))
            return {"run_id": run_id, "execution_state": row["execution_state"], "status": row["status"],
                "description": row["description"], "detail": row["detail"],
                "error_type": row["error_type"], "error_stage": row["error_stage"],
                "failure_location": failure_location, **arrays,
                "rule_coverage": _json(row["rule_coverage"], {}), "rule_baseline": _json(row["rule_baseline"], {}),
                "daily_summary": _json(row["daily_summary"], {}), "query_policy": _json(row["query_policy"], {}),
                "source_complete": None if row["source_complete"] is None else bool(row["source_complete"]),
                "observation": _json(row["observation"], {}),
                "counts": {name: row[name] for name in ("files", "evidence", "gaps", "propositions", "candidates")},
                "finding_count": row["finding_count"], "daily_issue_count": row["daily_issue_count"],
                "event_count": row["event_count"], "report_limit": 100}

    def files(self, run_id, *, offset=0, limit=20):
        _page(offset, limit)
        with self._read() as db:
            self._owner(db, run_id)
            cte = """WITH base AS (SELECT run_id,snapshot_json,acquisition_json FROM runs WHERE run_id=?),
                acquired AS (SELECT value FROM base,json_each(snapshot_json,'$.files')),
                pending AS (SELECT json_object('file_id',json_extract(q.value,'$.attachment_id'),
                    'original_name',json_extract(q.value,'$.attachment_name'),
                    'material_group',json_extract(q.value,'$.material_group'),'role',json_extract(q.value,'$.role'),
                    'report_date',json_extract(q.value,'$.report_date'),
                    'error',(SELECT json_extract(f.value,'$.error') FROM json_each(snapshot_json,'$.attachment_acquisition.failures') AS f
                      WHERE json_extract(f.value,'$.attachment_id')=json_extract(q.value,'$.attachment_id') LIMIT 1),'status',
                    CASE WHEN EXISTS(SELECT 1 FROM json_each(snapshot_json,'$.attachment_acquisition.failures') AS f
                      WHERE json_extract(f.value,'$.attachment_id')=json_extract(q.value,'$.attachment_id')) THEN 'failed' ELSE 'queued' END,
                    'source_metadata',json_object('attachment_id',json_extract(q.value,'$.attachment_id'),
                       'file_name_id',json_extract(q.value,'$.file_name_id'),'attachment_url',json_extract(q.value,'$.attachment_url')))
                    AS value FROM base,json_each(coalesce(json_extract(snapshot_json,'$.acquisition.rows'),json_extract(acquisition_json,'$.rows'))) AS q
                    WHERE NOT EXISTS(SELECT 1 FROM acquired WHERE json_extract(acquired.value,'$.file_id')=json_extract(q.value,'$.attachment_id'))),
                all_files AS (SELECT value FROM acquired UNION ALL SELECT value FROM pending)
                """
            total = db.execute(cte + "SELECT count(*) FROM all_files", (run_id,)).fetchone()[0]
            rows = db.execute(cte + """SELECT value,(SELECT count(*) FROM base,json_each(snapshot_json,'$.evidence') AS e
                 WHERE json_extract(e.value,'$.file_id')=json_extract(all_files.value,'$.file_id')
                    OR (json_extract(e.value,'$.file_id') IS NULL AND json_extract(e.value,'$.content_hash')=json_extract(all_files.value,'$.content_hash'))) AS evidence_count
                 FROM all_files LIMIT ? OFFSET ?""", (run_id, limit, offset)).fetchall()
            items = []
            fields = ("file_id", "original_name", "material_group", "role", "content_hash", "status", "byte_size", "report_date", "error")
            metadata_fields = ("attachment_id", "file_name_id", "attachment_url", "original_url", "report_date",
                               "report_time_year", "report_time_month", "attachment_cycle", "snapshot_id")
            for row in rows:
                file = _json(row["value"], {})
                item = {name: file.get(name) for name in fields}
                if not item["original_name"]:
                    item["original_name"] = str(file.get("source_path") or file.get("file_id") or "附件").replace("\\", "/").rsplit("/", 1)[-1]
                metadata = file.get("source_metadata") or {}
                item["source_metadata"] = {name: metadata[name] for name in metadata_fields if name in metadata}
                item["evidence_count"] = row["evidence_count"]
                items.append(item)
            pending = [item for item in items if item["status"] == "queued"]
            if pending:
                marks = ",".join("?" for _ in pending)
                latest_files = db.execute("""WITH events AS (
                    SELECT event_json,json_extract(event_json,'$.file_id') AS file_id,
                      json_extract(event_json,'$.stage') AS stage,
                      row_number() OVER (PARTITION BY json_extract(event_json,'$.file_id'),json_extract(event_json,'$.stage')
                        ORDER BY sequence DESC) AS position
                    FROM run_progress WHERE run_id=? AND json_extract(event_json,'$.file_id') IN (""" + marks + """ )
                      AND ((json_extract(event_json,'$.stage')='source' AND json_extract(event_json,'$.event') IN ('file_started','file_finished'))
                        OR (json_extract(event_json,'$.stage')='acquisition' AND json_extract(event_json,'$.event') IN ('attachment_started','attachment_finished'))))
                    SELECT file_id,stage,event_json FROM events WHERE position=1""",
                    (run_id, *(item["file_id"] for item in pending))).fetchall()
                by_file = {}
                for event in latest_files:
                    by_file.setdefault(event["file_id"], {})[event["stage"]] = _json(event["event_json"], {})
                for item in pending:
                    states = by_file.get(item["file_id"], {})
                    event = states.get("source") or states.get("acquisition")
                    if not event:
                        continue
                    if event.get("stage") == "source":
                        item["status"] = event.get("status", "acquired") if event.get("event") == "file_finished" else "running"
                    else:
                        item["status"] = ("failed" if event.get("status") == "failed" else
                                          "downloaded" if event.get("event") == "attachment_finished" else "downloading")
                    if event.get("issues"):
                        item["error"] = str(event["issues"][0])
            return _response(items, total, offset, limit)

    def evidence(self, run_id, *, offset=0, limit=20, file_id=None, refs=None):
        _page(offset, limit)
        filters, filter_values = [], []
        if file_id is not None:
            if not isinstance(file_id, str) or not file_id or len(file_id) > 256:
                raise InvalidRequest("file_id must be a short non-empty string")
            filters.append(_EVIDENCE_FILE + "=?")
            filter_values.append(file_id)
        if refs is not None:
            ids = list(dict.fromkeys(ref.strip() for ref in refs.split(",") if ref.strip()))
            if not 1 <= len(ids) <= 100 or any(len(ref) > 256 for ref in ids):
                raise InvalidRequest("refs must contain between 1 and 100 evidence IDs")
            marks = ",".join("?" for _ in ids)
            filters.append("(json_extract(e.value,'$.evidence_id') IN (" + marks + ") OR json_extract(e.value,'$.candidate_id') IN (" + marks + "))")
            filter_values.extend([*ids, *ids])
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._read() as db:
            self._owner(db, run_id)
            records = "SELECT e.value,0 AS is_candidate,0 AS source_order,CAST(e.key AS INTEGER) AS position FROM base,json_each(snapshot_json,'$.evidence') AS e"
            if refs is not None:
                # Explicitly cited candidate IDs remain inspectable, while the
                # default evidence list contains registered evidence only.
                records += (" UNION ALL SELECT e.value,1,1,CAST(e.key AS INTEGER) FROM base,json_each(snapshot_json,'$.daily_candidate_evidence') AS e"
                            " UNION ALL SELECT e.value,1,2,CAST(e.key AS INTEGER) FROM base,json_each(snapshot_json,'$.candidates') AS e")
            cte = "WITH base AS (SELECT snapshot_json FROM runs WHERE run_id=?),records AS (" + records + ") "
            base = " FROM base,records AS e" + where
            values = [run_id, *filter_values]
            total = db.execute(cte + "SELECT count(*)" + base, values).fetchone()[0]
            rows = db.execute(cte + "SELECT e.value,e.is_candidate," + _EVIDENCE_FILE + " AS resolved_file,"
                              "(SELECT coalesce(json_extract(f.value,'$.original_name'),json_extract(f.value,'$.file_id')) "
                              "FROM json_each(snapshot_json,'$.files') AS f WHERE json_extract(f.value,'$.file_id')=" + _EVIDENCE_FILE + " LIMIT 1) AS original_name" +
                              base + " ORDER BY e.source_order,e.position LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
            fields = ("evidence_id", "file_id", "text", "locator", "source_role", "material_group", "content_hash", "kind")
            items = []
            for row in rows:
                evidence = _json(row["value"], {})
                item = {name: evidence.get(name) for name in fields}
                item["evidence_id"] = (evidence.get("candidate_id") or evidence.get("evidence_id")) if row["is_candidate"] else evidence.get("evidence_id")
                item["file_id"] = row["resolved_file"]
                item["text"] = evidence.get("text") or evidence.get("quote") or ""
                item["locator"] = evidence.get("locator") or (evidence.get("metadata") or {}).get("locator", {})
                item["original_name"] = evidence.get("original_name") or row["original_name"] or item["file_id"]
                item["is_candidate"] = bool(row["is_candidate"])
                item["quality"] = "candidate" if item["is_candidate"] else (evidence.get("metadata") or {}).get("recognition_quality", "native")
                items.append(item)
            return _response(items, total, offset, limit)

    def diagnostics(self, run_id, *, section="gaps", offset=0, limit=20):
        _page(offset, limit)
        arrays = {
            "gaps": "coalesce(json_extract(result_json,'$.coverage_gaps'),json_extract(snapshot_json,'$.coverage_gaps'),'[]')",
            "propositions": "coalesce(json_extract(result_json,'$.propositions'),'[]')",
            "daily_episodes": "coalesce(json_extract(result_json,'$.daily_episodes'),'[]')",
            "findings": "coalesce(json_extract(result_json,'$.findings'),'[]')",
            "daily_issues": "coalesce(json_extract(result_json,'$.daily_issues'),'[]')",
        }
        if section not in arrays and section != "candidates":
            raise InvalidRequest("unsupported diagnostics section")
        with self._read() as db:
            self._owner(db, run_id)
            if section == "candidates":
                combined = ("SELECT e.value,e.type FROM runs,json_each(snapshot_json,'$.candidates') AS e WHERE run_id=? "
                            "UNION ALL SELECT e.value,e.type FROM runs,json_each(snapshot_json,'$.daily_candidate_evidence') AS e WHERE run_id=?")
                total = db.execute("SELECT count(*) FROM (" + combined + ")", (run_id, run_id)).fetchone()[0]
                rows = db.execute(combined + " LIMIT ? OFFSET ?", (run_id, run_id, limit, offset)).fetchall()
            else:
                expression = arrays[section]
                total = db.execute("SELECT json_array_length(" + expression + ") FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                rows = db.execute("SELECT e.value,e.type FROM runs,json_each(" + expression + ") AS e WHERE run_id=? LIMIT ? OFFSET ?",
                                  (run_id, limit, offset)).fetchall()
            items = [_json(row["value"]) if row["type"] in {"object", "array"} else row["value"] for row in rows]
            return {"section": section, **_response(items, total, offset, limit)}

    def progress(self, run_id, *, after=0, limit=100):
        with self._read() as db:
            owner = self._owner(db, run_id)
        return self.service.ledger.get_progress(run_id, owner["source_system"], owner["mine_code"], owner["environment"],
                                                after=after, limit=limit)


def register_debug_views(app, service):
    if getattr(app.state, "debug_views", None) is not None:
        return app.state.debug_views
    views = DebugViews(service)
    app.state.debug_views = views

    @app.get("/v1/debug/tasks/{record_id}")
    def task_detail(record_id: str):
        return views.task(record_id)

    @app.get("/v1/debug/runs/{run_id}/report")
    def run_report(run_id: str):
        return views.report(run_id)

    @app.get("/v1/debug/runs/{run_id}/files")
    def run_files(run_id: str, offset: int = 0, limit: int = 20):
        return views.files(run_id, offset=offset, limit=limit)

    @app.get("/v1/debug/runs/{run_id}/evidence")
    def run_evidence(run_id: str, offset: int = 0, limit: int = 20, file_id: str | None = None, refs: str | None = None):
        return views.evidence(run_id, offset=offset, limit=limit, file_id=file_id, refs=refs)

    @app.get("/v1/debug/runs/{run_id}/diagnostics")
    def run_diagnostics(run_id: str, section: str = "gaps", offset: int = 0, limit: int = 20):
        return views.diagnostics(run_id, section=section, offset=offset, limit=limit)

    @app.get("/v1/debug/runs/{run_id}/progress")
    def run_progress(run_id: str, after: int = 0, limit: int = 100):
        return views.progress(run_id, after=after, limit=limit)

    return views
