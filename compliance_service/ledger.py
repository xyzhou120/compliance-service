"""Durable SQLite ledger for the M0/M1 compliance service.

The ledger deliberately contains no business re-judgement API.  A run and its
result are immutable once finished; retries only concern delivery of that
result.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from . import contracts


class Ledger:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self._tx() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError(f"unsupported ledger schema version {version}")
            schema = """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  scope_key TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  created REAL NOT NULL,
                  execution_state TEXT NOT NULL,
                  worker_id TEXT,
                  lease_until REAL,
                  fence INTEGER NOT NULL DEFAULT 0,
                  snapshot_json TEXT,
                  result_json TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_scope
                  ON runs(scope_key)
                  WHERE execution_state IN ('queued','running');
                CREATE TABLE IF NOT EXISTS outbox (
                  delivery_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  worker_id TEXT,
                  lease_until REAL,
                  fence INTEGER NOT NULL DEFAULT 0,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  available_at REAL NOT NULL,
                  last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS mock_receipts (
                  delivery_id TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_progress (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  created REAL NOT NULL,
                  fence INTEGER NOT NULL,
                  event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_run_progress_run_sequence
                  ON run_progress(run_id, sequence);
                CREATE TABLE IF NOT EXISTS inspection_tasks (
                  task_key TEXT PRIMARY KEY,
                  source_system TEXT NOT NULL,
                  mine_code TEXT NOT NULL,
                  environment TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  identity_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_controls (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_key TEXT NOT NULL REFERENCES inspection_tasks(task_key),
                  created REAL NOT NULL,
                  action TEXT NOT NULL,
                  request_json TEXT NOT NULL
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            # Keep development v1 ledgers written before error diagnostics usable.
            if "last_error" not in {r[1] for r in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN last_error TEXT")
            for name, definition in {
                "task_key": "TEXT", "control_state": "TEXT NOT NULL DEFAULT 'running'",
                "acquisition_json": "TEXT", "execution_attempt": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            if "callback_url" not in {r[1] for r in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN callback_url TEXT")
            if version < 2:
                db.execute("DROP INDEX IF EXISTS uq_active_scope")
                db.execute("CREATE UNIQUE INDEX uq_active_scope ON runs(scope_key) WHERE execution_state IN ('queued','running','paused')")
                db.execute("UPDATE runs SET execution_attempt=fence")
                db.execute("PRAGMA user_version=2")

    @staticmethod
    def _json(value: Any) -> str:
        return contracts.canonical(value)

    @staticmethod
    def _decode(value: str | None):
        return json.loads(value) if value is not None else None

    def _run_dict(self, row: sqlite3.Row) -> dict:
        return {
            "run_id": row["run_id"], "created": row["created"],
            "execution_state": row["execution_state"],
            "spec": self._decode(row["spec_json"]),
            "snapshot": self._decode(row["snapshot_json"]),
            "result": self._decode(row["result_json"]),
            "fence": row["fence"], "worker_id": row["worker_id"],
            "lease_until": row["lease_until"],
            "task_key": row["task_key"], "control_state": row["control_state"],
            "acquisition": self._decode(row["acquisition_json"]),
            "execution_attempt": row["execution_attempt"],
        }

    def accept_task(self, task: dict, specs: list[contracts.RunSpec]) -> dict:
        """Persist the raw task and all item runs atomically, including after completion."""
        if not specs or any(s.task_key != task["task_key"] for s in specs):
            raise contracts.InvalidRequest("task identity mismatch")
        first = specs[0]
        identity = {"source_system": first.source_system, "mine_code": first.mine_code,
                    "environment": first.environment, "check_cycle": first.check_cycle,
                    "items": sorted(s.item_code for s in specs), "callback_url": task["callback_url"]}
        if len(identity["items"]) != len(set(identity["items"])):
            raise contracts.InvalidRequest("duplicate task item")
        with self._tx() as db:
            prior = db.execute("SELECT * FROM inspection_tasks WHERE task_key=?", (task["task_key"],)).fetchone()
            created = prior is None
            if prior and self._decode(prior["identity_json"]) != identity:
                raise contracts.Conflict("task identity or callback URL changed; use a new execution identity")
            if created:
                db.execute("INSERT INTO inspection_tasks VALUES(?,?,?,?,?,?,?,?)", (
                    task["task_key"], first.source_system, first.mine_code, first.environment,
                    self._json(task["raw"]), self._json(identity), "running", self.clock()))
                for spec in specs:
                    db.execute("INSERT INTO runs(run_id,scope_key,spec_json,created,execution_state,task_key) VALUES(?,?,?,?,?,?)", (
                        uuid.uuid4().hex, spec.scope_key, self._json(spec.to_dict()), self.clock(), "queued", task["task_key"]))
            db.execute("INSERT INTO task_controls(task_key,created,action,request_json) VALUES(?,?,?,?)", (
                task["task_key"], self.clock(), "running", self._json(task["raw"])))
            rows = db.execute("SELECT * FROM runs WHERE task_key=? ORDER BY created,run_id", (task["task_key"],)).fetchall()
            return {"task_key": task["task_key"], "created": created, "items": [
                {"run_id": row["run_id"], "item_code": self._decode(row["spec_json"])["item_code"],
                 "created": created, "execution_state": row["execution_state"]} for row in rows]}

    def get_task(self, task_key, source_system, mine_code, environment):
        with self._tx() as db:
            row = db.execute("SELECT * FROM inspection_tasks WHERE task_key=? AND source_system=? AND mine_code=? AND environment=?",
                             (task_key, source_system, mine_code, environment)).fetchone()
            if row is None:
                raise contracts.NotFound("task not found")
            runs = db.execute("SELECT runs.*,outbox.state AS delivery_state,outbox.attempt AS delivery_attempt,outbox.last_error AS delivery_error "
                              "FROM runs LEFT JOIN outbox ON runs.run_id=outbox.run_id WHERE task_key=? ORDER BY created,runs.run_id", (task_key,)).fetchall()
            return {"task_key": task_key, "raw": self._decode(row["request_json"]), "state": row["state"],
                    "items": [{**self._run_dict(r), "delivery_state": r["delivery_state"] or "none",
                               "delivery_attempt": r["delivery_attempt"] or 0, "delivery_error": r["delivery_error"]} for r in runs]}

    def list_executions(self, *, mine_code=None, check_cycle=None, offset=0, limit=20):
        """Page compact task summaries without reading document snapshots."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise contracts.InvalidRequest("offset must be non-negative and limit must be between 1 and 100")
        clauses = ["json_extract(spec_json,'$.environment') IN ('local','test')",
                   "json_extract(spec_json,'$.callback_mode') IN ('none','mock')"]
        params = []
        for field, value in (("mine_code", mine_code), ("check_cycle", check_cycle)):
            if value is not None:
                contracts.required_text(value, field)
                clauses.append(f"json_extract(spec_json,'$.{field}')=?")
                params.append(value)
        eligible = ("SELECT run_id,created,execution_state,spec_json,result_json,"
                    "coalesce(task_key,run_id) AS record_id FROM runs WHERE " + " AND ".join(clauses))
        grouped = "SELECT record_id,min(created) AS submitted FROM eligible GROUP BY record_id"
        with self._tx() as db:
            total = db.execute("WITH eligible AS (" + eligible + ") SELECT count(*) FROM (" + grouped + ")", params).fetchone()[0]
            pages = db.execute("WITH eligible AS (" + eligible + ") " + grouped +
                               " ORDER BY submitted DESC,record_id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            if not pages:
                return {"records": [], "total": total, "offset": offset, "limit": limit, "has_more": False}
            ids = [row["record_id"] for row in pages]
            marks = ",".join("?" for _ in ids)
            rows = db.execute("WITH eligible AS (" + eligible + ") SELECT record_id,run_id,created,execution_state,"
                              "json_extract(spec_json,'$.source_system') AS source_system,"
                              "json_extract(spec_json,'$.mine_code') AS mine_code,"
                              "json_extract(spec_json,'$.environment') AS environment,"
                              "json_extract(spec_json,'$.check_cycle') AS check_cycle,"
                              "json_extract(spec_json,'$.item_code') AS item_code,"
                              "json_extract(spec_json,'$.inspection_item.name') AS item_name,"
                              "json_extract(result_json,'$.status') AS result_status,"
                              "json_extract(result_json,'$.description') AS description,"
                              "(SELECT json_object('sequence',sequence,'created',created,'event',json(event_json)) FROM run_progress "
                              "WHERE run_progress.run_id=eligible.run_id ORDER BY sequence DESC LIMIT 1) AS latest_event,"
                              "(SELECT min(created) FROM run_progress WHERE run_progress.run_id=eligible.run_id "
                              "AND json_extract(event_json,'$.event')='claimed') AS started_at,"
                              "(SELECT max(created) FROM run_progress WHERE run_progress.run_id=eligible.run_id "
                              "AND json_extract(event_json,'$.stage')='run' AND json_extract(event_json,'$.event')='finished') AS finished_at "
                              f"FROM eligible WHERE record_id IN ({marks}) ORDER BY created,run_id", [*params, *ids]).fetchall()
            by_id = {key: [] for key in ids}
            for row in rows:
                by_id[row["record_id"]].append(row)
            records = []
            for page in pages:
                members = by_id[page["record_id"]]
                first = members[0]
                states = {r["execution_state"] for r in members}
                state = next((s for s in ("running", "queued", "paused", "failed") if s in states), "completed")
                records.append({"record_id": page["record_id"], "created": page["submitted"],
                    "source_system": first["source_system"], "mine_code": first["mine_code"],
                    "environment": first["environment"], "check_cycle": first["check_cycle"],
                    "execution_state": state, "item_count": len(members),
                    "completed_count": sum(r["execution_state"] in {"completed", "failed"} for r in members),
                    "items": [{"run_id": r["run_id"], "item_code": r["item_code"],
                               "item_name": r["item_name"] or r["item_code"],
                               "execution_state": r["execution_state"], "result_status": r["result_status"],
                               "elapsed_ms": round(max(0, (r["finished_at"] or self.clock()) - r["started_at"]) * 1000, 2) if r["started_at"] else None,
                               "latest_event": self._decode(r["latest_event"]),
                               "description": r["description"]} for r in members]})
            return {"records": records, "total": total, "offset": offset, "limit": limit, "has_more": offset+limit < total}

    def control_task(self, task_key, source_system, mine_code, environment, action, raw=None):
        if action not in {"pause", "resume"}:
            raise contracts.InvalidRequest("unsupported control action")
        with self._tx() as db:
            row = db.execute("SELECT * FROM inspection_tasks WHERE task_key=? AND source_system=? AND mine_code=? AND environment=?",
                             (task_key, source_system, mine_code, environment)).fetchone()
            if row is None:
                raise contracts.NotFound("task not found")
            active = db.execute("SELECT count(*) FROM runs WHERE task_key=? AND execution_state IN ('queued','running','paused')", (task_key,)).fetchone()[0]
            if not active:
                raise contracts.Conflict("terminal task cannot be paused or resumed; create a new execution")
            if action == "pause":
                db.execute("UPDATE runs SET control_state='paused',execution_state=CASE WHEN execution_state='queued' THEN 'paused' ELSE execution_state END WHERE task_key=? AND execution_state IN ('queued','running','paused')", (task_key,))
            else:
                db.execute("UPDATE runs SET control_state='running',execution_state=CASE WHEN execution_state='paused' THEN 'queued' ELSE execution_state END WHERE task_key=? AND execution_state IN ('queued','running','paused')", (task_key,))
            db.execute("UPDATE inspection_tasks SET state=? WHERE task_key=?", ("paused" if action == "pause" else "running", task_key))
            db.execute("INSERT INTO task_controls(task_key,created,action,request_json) VALUES(?,?,?,?)",
                       (task_key, self.clock(), action, self._json(raw or {})))

    def checkpoint(self, run_id, worker_id, fence):
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise contracts.NotFound("run not found")
            self._lease(row, worker_id, fence, self.clock())
            if row["control_state"] == "paused":
                raise contracts.RunPaused()

    def pause_run(self, run_id, worker_id, fence):
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            self._lease(row, worker_id, fence, self.clock())
            if row["control_state"] == "paused":
                db.execute("UPDATE runs SET execution_state='paused',worker_id=NULL,lease_until=NULL,execution_attempt=max(0,execution_attempt-1) WHERE run_id=?", (run_id,))
            else:
                db.execute("UPDATE runs SET execution_state='queued',worker_id=NULL,lease_until=NULL,execution_attempt=max(0,execution_attempt-1) WHERE run_id=?", (run_id,))

    def save_acquisition(self, run_id, worker_id, fence, snapshot):
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            self._lease(row, worker_id, fence, self.clock())
            if row["acquisition_json"] is not None:
                if self._decode(row["acquisition_json"]) != snapshot:
                    raise contracts.Conflict("query snapshot already frozen")
                return
            spec = self._decode(row["spec_json"])
            if snapshot.get("mine_code") != spec["mine_code"]:
                raise contracts.InvalidRequest("query snapshot mine mismatch")
            db.execute("UPDATE runs SET acquisition_json=? WHERE run_id=?", (self._json(snapshot), run_id))

    def accept(self, spec: contracts.RunSpec) -> dict:
        run_id = uuid.uuid4().hex
        with self._tx() as db:
            existing = db.execute("SELECT * FROM runs WHERE scope_key=? AND execution_state IN ('queued','running','paused')", (spec.scope_key,)).fetchone()
            if existing is not None:
                if self._decode(existing["spec_json"])["callback_mode"] != spec.callback_mode:
                    raise contracts.Conflict("active scope uses a different callback mode")
                return {"run_id": existing["run_id"], "created": False, "execution_state": existing["execution_state"]}
            db.execute(
                "INSERT INTO runs(run_id,scope_key,spec_json,created,execution_state) VALUES(?,?,?,?,?)",
                (run_id, spec.scope_key, self._json(spec.to_dict()), self.clock(), "queued"),
            )
        return {"run_id": run_id, "created": True, "execution_state": "queued"}

    def _owned_row(self, db, run_id, source_system, mine_code, environment):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise contracts.NotFound("run not found")
        spec = self._decode(row["spec_json"])
        if (spec.get("source_system"), spec.get("mine_code"), spec.get("environment")) != (source_system, mine_code, environment):
            raise contracts.NotFound("run not found")
        return row

    def get(self, run_id: str, source_system: str, mine_code: str, environment: str) -> dict:
        with self._tx() as db:
            row = self._owned_row(db, run_id, source_system, mine_code, environment)
            out = self._run_dict(row)
            delivery = db.execute("SELECT state,last_error,attempt FROM outbox WHERE run_id=?", (run_id,)).fetchone()
            out["delivery_state"] = delivery["state"] if delivery else "none"
            out["delivery_error"] = delivery["last_error"] if delivery else None
            out["delivery_attempt"] = delivery["attempt"] if delivery else 0
            progress = db.execute(
                "SELECT count(*) AS count, max(sequence) AS latest FROM run_progress WHERE run_id=?",
                (run_id,),
            ).fetchone()
            out["progress_summary"] = {"event_count": progress["count"], "latest_sequence": progress["latest"]}
            return out

    @staticmethod
    def _progress_event(event: dict) -> dict:
        if not isinstance(event, dict):
            raise contracts.InvalidRequest("progress event must be an object")
        # Keep the event contract deliberately small: no headers, credentials,
        # local paths, or arbitrary provider configuration can enter the ledger.
        allowed = {"stage", "event", "file_id", "page", "page_total", "provider",
                   "elapsed_ms", "status", "issues", "file_index", "file_total", "file_name",
                   "evidence_count", "fact_count", "candidate_count"}
        out = {key: event[key] for key in allowed if key in event and event[key] is not None}
        for key in ("stage", "event", "file_id", "provider", "status", "file_name"):
            if key in out and (not isinstance(out[key], str) or len(out[key]) > 256):
                raise contracts.InvalidRequest(f"progress {key} must be a short string")
        for key in ("page", "page_total", "elapsed_ms", "file_index", "file_total",
                    "evidence_count", "fact_count", "candidate_count"):
            if key in out and (type(out[key]) not in (int, float) or not math.isfinite(out[key]) or out[key] < 0):
                raise contracts.InvalidRequest(f"progress {key} must be non-negative")
        if "issues" in out:
            if not isinstance(out["issues"], list) or len(out["issues"]) > 50:
                raise contracts.InvalidRequest("progress issues must be a short list")
            out["issues"] = [str(item)[:512] for item in out["issues"]]
        return out

    def record_progress(self, run_id: str, worker_id: str, fence: int, event: dict) -> dict:
        """Append one lease-fenced, non-sensitive progress event."""
        payload = self._progress_event(event)
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise contracts.NotFound("run not found")
            self._lease(row, worker_id, fence, now)
            if row["control_state"] == "paused":
                raise contracts.RunPaused()
            cursor = db.execute(
                "INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",
                (run_id, now, fence, self._json(payload)),
            )
            return {"sequence": cursor.lastrowid, "created": now, "fence": fence, **payload}

    def get_progress(self, run_id: str, source_system: str, mine_code: str, environment: str,
                     *, after: int = 0, limit: int = 100) -> dict:
        if type(after) is not int or after < 0:
            raise contracts.InvalidRequest("after must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise contracts.InvalidRequest("limit must be between 1 and 500")
        with self._tx() as db:
            run = self._owned_row(db, run_id, source_system, mine_code, environment)
            rows = db.execute(
                "SELECT sequence,created,fence,event_json FROM run_progress "
                "WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            events = []
            for row in rows:
                events.append({"sequence": row["sequence"], "created": row["created"],
                               "fence": row["fence"], **self._decode(row["event_json"])})
            next_after = events[-1]["sequence"] if events else after
            return {"run_id": run_id, "execution_state": run["execution_state"],
                    "events": events, "next_after": next_after,
                    "has_more": has_more}

    def _lease(self, row, worker_id, fence, now):
        if row["execution_state"] != "running" or row["worker_id"] != worker_id or row["fence"] != fence or row["lease_until"] is None or row["lease_until"] <= now:
            raise contracts.StaleLease("worker lease is stale or expired")

    def claim(self, worker_id: str, lease_seconds: float = 30, *, run_id: str | None = None,
              debug_only: bool = False) -> dict | None:
        self._validate_lease(worker_id, lease_seconds)
        with self._tx() as db:
            now = self.clock()
            sql = "SELECT * FROM runs WHERE control_state='running' AND (execution_state='queued' OR (execution_state='running' AND lease_until<=?))"
            params = [now]
            if debug_only:
                sql += " AND task_key LIKE 'debug-%' AND json_extract(spec_json,'$.callback_mode')='none'"
            if run_id is not None:
                sql += " AND run_id=?"
                params.append(run_id)
            row = db.execute(sql + " ORDER BY created LIMIT 1", params).fetchone()
            if row is None:
                return None
            fence = row["fence"] + 1
            db.execute("UPDATE runs SET execution_state='running',worker_id=?,lease_until=?,fence=?,execution_attempt=execution_attempt+1 WHERE run_id=?", (worker_id, now + lease_seconds, fence, row["run_id"]))
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            return self._run_dict(row)

    def save_snapshot(self, run_id: str, worker_id: str, fence: int, snapshot: dict) -> dict:
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise contracts.NotFound("run not found")
            self._lease(row, worker_id, fence, now)
            if row["snapshot_json"] is not None:
                raise contracts.Conflict("snapshot already saved")
            run = self._run_dict(row)
            contracts.assert_snapshot_owner(snapshot, run)
            db.execute("UPDATE runs SET snapshot_json=? WHERE run_id=?", (self._json(snapshot), run_id))
            return snapshot

    def finish(self, run_id: str, worker_id: str, fence: int, result: dict, delivery_payload: dict | None = None) -> dict:
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise contracts.NotFound("run not found")
            self._lease(row, worker_id, fence, now)
            if row["control_state"] == "paused":
                raise contracts.RunPaused()
            run = self._run_dict(row)
            snapshot = run["snapshot"]
            contracts.validate_result(result, run, snapshot)
            if run["spec"]["callback_mode"] == "real" and delivery_payload is None:
                raise contracts.InvalidRequest("real callback requires a finalized external DTO")
            state = "failed" if result["status"] == "failed" else "completed"
            db.execute("INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",
                       (run_id, now, fence, self._json({"stage": "run", "event": "finished", "status": result["status"]})))
            db.execute("UPDATE runs SET execution_state=?,result_json=?,worker_id=NULL,lease_until=NULL WHERE run_id=?", (state, self._json(result), run_id))
            if row["task_key"]:
                remaining = db.execute("SELECT count(*) FROM runs WHERE task_key=? AND execution_state IN ('queued','running','paused')", (row["task_key"],)).fetchone()[0]
                if not remaining:
                    failed = db.execute("SELECT count(*) FROM runs WHERE task_key=? AND execution_state='failed'", (row["task_key"],)).fetchone()[0]
                    db.execute("UPDATE inspection_tasks SET state=? WHERE task_key=?", ("failed" if failed else "completed", row["task_key"]))
            if run["spec"]["callback_mode"] in {"mock", "real"}:
                db.execute("INSERT INTO outbox(delivery_id,run_id,payload_json,state,available_at,callback_url) VALUES(?,?,?,?,?,?)",
                           (uuid.uuid4().hex, run_id, self._json(delivery_payload if delivery_payload is not None else result),
                            "waiting", now, run["spec"].get("callback_url")))
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._run_dict(row)

    def renew(self, run_id: str, worker_id: str, fence: int, lease_seconds: float = 30) -> dict:
        self._validate_lease(worker_id, lease_seconds)
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None: raise contracts.NotFound("run not found")
            self._lease(row, worker_id, fence, now)
            db.execute("UPDATE runs SET lease_until=? WHERE run_id=?", (now + lease_seconds, run_id))
            return {"run_id": run_id, "fence": fence, "lease_until": now + lease_seconds}

    def claim_delivery(self, worker_id: str, lease_seconds: float = 30) -> dict | None:
        self._validate_lease(worker_id, lease_seconds)
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM outbox WHERE (state='waiting' AND available_at<=?) OR (state='sending' AND lease_until<=?) ORDER BY available_at LIMIT 1", (now, now)).fetchone()
            if row is None: return None
            fence = row["fence"] + 1
            attempt = row["attempt"] + 1
            db.execute("UPDATE outbox SET state='sending',worker_id=?,lease_until=?,fence=?,attempt=? WHERE delivery_id=?", (worker_id, now + lease_seconds, fence, attempt, row["delivery_id"]))
            return {"delivery_id": row["delivery_id"], "run_id": row["run_id"], "payload": self._decode(row["payload_json"]),
                    "callback_url": row["callback_url"], "attempt": attempt, "fence": fence}

    def complete_delivery(self, delivery_id: str, worker_id: str, fence: int, error: str | None = None, permanent: bool = False, elapsed_ms: float = 0):
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row is None: raise contracts.NotFound("delivery not found")
            if row["state"] != "sending" or row["worker_id"] != worker_id or row["fence"] != fence or row["lease_until"] is None or row["lease_until"] <= now:
                raise contracts.StaleLease("delivery lease is stale or expired")
            if error is not None:
                delay = min(3600, 2 ** min(row["attempt"], 10))
                state = "failed" if permanent else "waiting"
                db.execute("UPDATE outbox SET state=?,worker_id=NULL,lease_until=NULL,available_at=?,last_error=? WHERE delivery_id=?", (state, now + delay, str(error), delivery_id))
            else:
                db.execute("UPDATE outbox SET state='delivered',worker_id=NULL,lease_until=NULL,last_error=NULL WHERE delivery_id=?", (delivery_id,))
            db.execute("INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",
                       (row["run_id"], now, fence, self._json({"stage": "callback", "event": "finished",
                        "status": ("failed" if permanent else "waiting") if error is not None else "delivered",
                        "elapsed_ms": round(elapsed_ms, 3), "issues": [error] if error else []})))

    def record_mock_delivery(self, delivery_id: str, payload: dict):
        with self._tx() as db:
            row = db.execute("SELECT payload_json FROM mock_receipts WHERE delivery_id=?", (delivery_id,)).fetchone()
            encoded = self._json(payload)
            if row is not None:
                if row[0] != encoded: raise contracts.Conflict("mock receipt payload mismatch")
                return
            outbox = db.execute("SELECT payload_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
            if outbox is None:
                raise contracts.NotFound("delivery not found")
            if outbox[0] != encoded:
                raise contracts.Conflict("mock receipt does not match saved payload")
            db.execute("INSERT INTO mock_receipts(delivery_id,payload_json,created) VALUES(?,?,?)", (delivery_id, encoded, self.clock()))

    def list_mock_receipts(self) -> list:
        with self._tx() as db:
            rows = db.execute("SELECT delivery_id,payload_json,created FROM mock_receipts ORDER BY created,delivery_id").fetchall()
            return [{"delivery_id": r[0], "payload": self._decode(r[1]), "created": r[2]} for r in rows]

    @staticmethod
    def _validate_lease(worker_id, lease_seconds):
        contracts.required_text(worker_id, "worker_id")
        if not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds < float("inf"):
            raise contracts.InvalidRequest("lease_seconds must be positive and finite")

