"""Application use case shared by manual, upstream and command-line entries."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from time import perf_counter

from .contracts import Conflict, InvalidRequest, RunPaused, RunSpec, StaleLease, accepted_inspection_item, digest, parse_batch, rule_handler
from .fixtures import evaluate, load_snapshot, result_base


class InspectionService:
    def __init__(self, ledger, *, source=None, evaluator=None, fault_hook=None, max_attempts=3,
                 repository=None, acquisition=None, callback_client=None, status_map=None,
                 source_system="oceanbase", environment="test", allowed_mines=None,
                 inspection_registry=None, inspection_config_path=None):
        self.ledger = ledger
        self.source = source or self._source
        self.evaluator = evaluator or self._evaluate
        self.fault_hook = fault_hook or (lambda point: None)
        if type(max_attempts) is not int or max_attempts < 1:
            raise InvalidRequest("max_attempts must be a positive integer")
        self.max_attempts = max_attempts
        self.repository = repository
        self.acquisition = acquisition
        self.callback_client = callback_client
        self.status_map = status_map
        self.source_system = source_system
        self.environment = environment
        self.allowed_mines = set(allowed_mines or ())
        self.task_token = None
        self.external_worker = False
        from .inspection_config import InspectionConfigRegistry
        import os
        config_path = inspection_config_path or os.environ.get("COMPLIANCE_INSPECTION_CONFIG") or (Path(ledger.path).parent / "inspection-config.json")
        self.inspection_registry = inspection_registry or InspectionConfigRegistry(config_path)

    def inspection_config(self):
        return self.inspection_registry.read()

    def receive_task(self, body, *, allowed_mines):
        from .upstream import normalize_upstream_task
        config = self.inspection_registry.read()["config"]
        task = normalize_upstream_task(body, status_map=self.status_map, identity_only=True)
        if task["mine_code"] not in allowed_mines:
            from .contracts import NotFound
            raise NotFound("mine not authorized")
        if self.repository is None or self.acquisition is None or self.callback_client is None:
            raise InvalidRequest("upstream acquisition and callback adapters are not configured")
        from .oceanbase import _period
        _period(task["check_cycle"])
        from .callbacks import CallbackPermanentError
        try:
            self.callback_client.policy.validate(task["callback_url"])
        except CallbackPermanentError as exc:
            raise InvalidRequest(str(exc)) from exc
        task["task_key"] = digest({"source": self.source_system, "environment": self.environment,
                                    "task": task["task_key"]})
        if task["action"] != "running":
            stored = self.ledger.get_task(task["task_key"], self.source_system, task["mine_code"], self.environment)
            accepted = stored["raw"]
            if (accepted["resultCallbackUrl"] != task["callback_url"] or accepted["checkCycle"] != task["check_cycle"]
                    or sorted(i["itemCode"] for i in accepted["items"]) != sorted(i["item_code"] for i in task["items"])):
                raise Conflict("task scope and callback URL cannot change during task control")
            self.ledger.control_task(task["task_key"], self.source_system, task["mine_code"], self.environment,
                                     task["action"], task["raw"])
            return {"task_key": task["task_key"], "created": False}
        # Code validation applies to new admissions. Controls above resolve the
        # persisted execution even after its configured code has been renamed.
        from .upstream import _items
        _items(body.get("items"), inspection_config=config)
        specs = [RunSpec(self.source_system, task["mine_code"], item["item_code"], task["check_cycle"],
                         {"oceanbase": task["task_key"]}, environment=self.environment,
                         callback_mode="real", callback_url=task["callback_url"], task_key=task["task_key"],
                         inspection_item=accepted_inspection_item(config, item["item_code"], task["check_cycle"]))
                 for item in task["items"]]
        return self.ledger.accept_task(task, specs)

    def _source(self, run):
        remote = "oceanbase" in run["spec"]["scope"]
        if "manifest" in run["spec"]["scope"] or remote:
            from .sources import LocalDocumentSource
            callback = lambda event: self.ledger.record_progress(
                run["run_id"], run["worker_id"], run["fence"], event)
            query = None
            if remote:
                spec = run["spec"]
                query = run.get("acquisition")
                if query is None:
                    started = perf_counter()
                    from .oceanbase import OceanBaseRepository
                    options = {"inspection_config": spec["inspection_item"]} if isinstance(self.repository, OceanBaseRepository) and spec.get("inspection_item") else {}
                    query = self.repository.query(spec["mine_code"], spec["check_cycle"], spec["item_code"], **options)
                    self.ledger.save_acquisition(run["run_id"], run["worker_id"], run["fence"], query)
                    callback({"stage": "query", "event": "finished", "elapsed_ms": (perf_counter()-started)*1000})
                self.ledger.checkpoint(run["run_id"], run["worker_id"], run["fence"])
                manifest = self.acquisition.prepare(query, Path(self.ledger.path).parent / "inputs" / run["run_id"],
                                                    progress_callback=callback)
                run = {**run, "spec": {**spec, "scope": {"manifest": manifest}},
                       "catalog_profile": "oceanbase"}
            # The source owns parser/page instrumentation; the service only
            # supplies its lease-fenced sink.  Keep construction local so
            # fixture runs remain entirely offline and unchanged.
            source = LocalDocumentSource(Path(self.ledger.path).parent / "artifacts")
            snapshot = source(run, progress_callback=callback)
            if query is not None:
                snapshot["acquisition"] = query
                import json
                material = json.loads(Path(manifest).read_text(encoding="utf-8"))
                snapshot["attachment_acquisition"] = material.get("acquisition", {})
                for failure in snapshot["attachment_acquisition"].get("failures", []):
                    snapshot["coverage_gaps"].append(
                        f"{failure['attachment_id']}: attachment acquisition failed ({failure.get('error', failure.get('status'))})")
                snapshot["source_complete"] = False  # An index is not observation completeness.
            return snapshot
        return load_snapshot(run)

    @staticmethod
    def _evaluate(run, snapshot):
        if "manifest" in run["spec"]["scope"] or "oceanbase" in run["spec"]["scope"]:
            from .rules import evaluate_documents
            from hashlib import sha256
            from importlib.resources import files
            result = evaluate_documents(run, snapshot)
            from .scope_checks import evaluate_scope
            scope = evaluate_scope(run, snapshot)
            result["propositions"].extend(scope["propositions"])
            result["findings"].extend(scope["findings"])
            result["coverage_gaps"].extend(scope["gaps"])
            scope_refs = {ref for p in scope["propositions"] for ref in p["evidence_refs"]}
            result["evidence_refs"] = sorted(set(result["evidence_refs"]) | scope_refs)
            if scope["findings"]:
                result["status"] = "no_pass"
                result["description"] = "实际证据证明检查条件不满足"
            elif scope["gaps"] and result["status"] == "pass":
                result["status"] = "pending"
                result["description"] = "必要证据未闭合，本轮结束"
            from .rulebook import apply_draft_scope, rulebook_reference
            if snapshot.get("rule_baseline"):
                handler = rule_handler(run["spec"])
                result["rule_baseline"] = rulebook_reference(snapshot["rule_baseline"], handler)
                result = apply_draft_scope(result, snapshot, handler, implemented_ids=scope["implemented_ids"])
            result["association_links"] = snapshot.get("association_links", [])
            result["event_evidence_chains"] = InspectionService._evidence_chains(result, snapshot)
            result["fact_evidence_chains"] = InspectionService._fact_evidence_chains(snapshot)
            daily = snapshot.get("daily_assessment") or {}
            result["daily_clues"] = snapshot.get("daily_clues", [])
            result["daily_arrangements"] = snapshot.get("daily_arrangements", [])
            result["daily_issues"] = daily.get("issues", [])
            result["daily_episodes"] = daily.get("episodes", [])
            result["daily_summary"] = daily.get("summary", {})
            result["daily_candidate_evidence"] = snapshot.get("daily_candidate_evidence", [])
            daily_refs = {ref for issue in daily.get("issues", []) for ref in issue.get("evidence_refs", [])}
            registered = {entry["evidence_id"] for entry in snapshot["evidence"]}
            result["evidence_refs"] = sorted(set(result["evidence_refs"]) | (daily_refs & registered))
            from .rules import gate_conflicted_evidence
            result = gate_conflicted_evidence(result, snapshot)
            result["rule_revision"] = sha256(files("compliance_service").joinpath("rules.py").read_bytes()).hexdigest()
            return result
        return evaluate(run, snapshot)

    @staticmethod
    def _fact_evidence_chains(snapshot):
        evidence = {entry["evidence_id"]: entry for entry in snapshot["evidence"]}
        files = {entry["file_id"]: entry for entry in snapshot["files"]}
        events = {fact.get("event_id") for fact in snapshot.get("facts", [])
                  if fact.get("kind") == "event" and fact.get("event_id")}
        chains = []
        for fact in snapshot.get("facts", []):
            refs = [evidence[ref] for ref in fact.get("evidence_refs", [])]
            is_execution = fact["kind"] in {"action", "measurement"}
            bound = fact.get("event_id") in events if is_execution else None
            links = [link for link in snapshot.get("association_links", []) if link.get("fact_id") == fact["fact_id"]]
            candidate_binding = any(link.get("temporal_partition") for link in links)
            reasons = [gap for gap in snapshot.get("coverage_gaps", []) if fact["fact_id"] in gap]
            if is_execution and not bound:
                reasons.append("record is preserved as a fact; no verified trigger-event association is available")
            chains.append({
                "fact_id": fact["fact_id"], "kind": fact["kind"], "event_id": fact.get("event_id"),
                "workface": fact.get("workface"), "time": fact.get("time"), "fields": fact["fields"],
                "event_binding": "candidate" if candidate_binding else "bound" if bound else "unbound" if is_execution else "requirement",
                "association_links": links,
                "binding_gaps": list(dict.fromkeys(reasons)), "evidence_refs": fact.get("evidence_refs", []),
                "sources": [{"evidence_id": entry["evidence_id"], "file_id": entry["file_id"],
                             "original_name": files[entry["file_id"]].get("original_name", files[entry["file_id"]].get("source_path")),
                             "content_hash": entry["content_hash"], "locator": entry["locator"],
                             "quote": entry["text"], "source_role": entry["source_role"]} for entry in refs],
            })
        return chains

    @staticmethod
    def _evidence_chains(result, snapshot):
        evidence = {entry["evidence_id"]: entry for entry in snapshot["evidence"]}
        chains = []
        for event in result.get("events", []):
            propositions = [p for p in result.get("propositions", [])
                            if (p.get("event_id"), p.get("workface")) == (event["event_id"], event["workface"])]
            steps = []
            for proposition in propositions:
                refs = [evidence[ref] for ref in proposition["evidence_refs"]]
                steps.append({**proposition, "sources": [
                    {"evidence_id": e["evidence_id"], "file_id": e["file_id"], "content_hash": e["content_hash"],
                     "locator": e["locator"], "quote": e["text"], "source_role": e["source_role"]} for e in refs
                ]})
            chains.append({"event_id": event["event_id"], "workface": event["workface"], "steps": steps})
        return chains

    def submit(self, body: dict) -> dict:
        specs = parse_batch(body, inspection_config=self.inspection_registry.read()["config"])
        return {"items": [self.ledger.accept(spec) for spec in specs]}

    def submit_debug_task(self, body: dict) -> dict:
        """Accept an upstream-shaped task for local UI debugging only.

        The manifest is the locally frozen input.  Callback fields are retained
        only as display metadata; this path always uses callback_mode=none.
        """
        from .upstream import _items
        if self.task_token or self.environment == "production":
            raise InvalidRequest("local debug tasks are unavailable in production mode")
        if not isinstance(body, dict):
            raise InvalidRequest("debug task must be an object")
        allowed = {"mineId", "status", "checkCycle", "items", "resultCallbackUrl",
                   "manifestPath", "sourceSystem", "environment", "ruleVersion", "dataSource"}
        if set(body) - allowed:
            raise InvalidRequest("unknown debug task fields")
        if body.get("status", "running") != "running":
            raise InvalidRequest("local debug task supports status=running only")
        mine = body.get("mineId")
        source = body.get("sourceSystem")
        environment = body.get("environment")
        from .contracts import resolve_check_cycle
        cycle = resolve_check_cycle(body.get("checkCycle"))
        manifest = body.get("manifestPath")
        data_source = body.get("dataSource", "manifest")
        if data_source not in {"manifest", "oceanbase"}:
            raise InvalidRequest("dataSource must be manifest or oceanbase")
        if not isinstance(mine, str) or not mine.strip():
            raise InvalidRequest("mineId is required")
        if source is not None and (not isinstance(source, str) or not source.strip()):
            raise InvalidRequest("sourceSystem must be a non-empty string")
        if environment is not None and (not isinstance(environment, str) or not environment.strip()):
            raise InvalidRequest("environment must be a non-empty string")
        if data_source == "manifest" and (not isinstance(manifest, str) or not Path(manifest).is_absolute() or not Path(manifest).is_file()):
            raise InvalidRequest("manifestPath must be an existing absolute local JSON path")
        config = self.inspection_registry.read()["config"]
        items = _items(body.get("items"), inspection_config=config)
        from .oceanbase import _period
        _period(cycle)
        callback_url = body.get("resultCallbackUrl")
        if callback_url is not None and (not isinstance(callback_url, str) or len(callback_url) > 2048):
            raise InvalidRequest("resultCallbackUrl must be a short string")
        from uuid import uuid4
        task_key = "debug-" + uuid4().hex
        if data_source == "manifest":
            import json
            try:
                manifest_owner = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise InvalidRequest("manifestPath must contain readable JSON") from exc
            if not isinstance(manifest_owner, dict):
                raise InvalidRequest("manifest must be an object")
            source = source or manifest_owner.get("source_system")
            environment = environment or manifest_owner.get("environment")
        source = source or "debug-local"
        environment = environment or "local"
        if environment not in {"local", "test"}:
            raise InvalidRequest("debug tasks require local/test environment")
        if data_source == "oceanbase":
            if self.repository is None or self.acquisition is None:
                raise InvalidRequest("OceanBase debug acquisition is not configured; use a local manifest or local-debug config")
            if mine not in self.allowed_mines:
                raise InvalidRequest("mineId is outside the configured debug mine list")
            source, environment = self.source_system, self.environment
            specs = [RunSpec(source, mine, item["item_code"], cycle, {"oceanbase": task_key}, environment=environment,
                             callback_mode="none", task_key=task_key, rulebook_version=body.get("ruleVersion"),
                             inspection_item=accepted_inspection_item(config, item["item_code"], cycle)) for item in items]
            accepted = self.ledger.accept_task({"task_key": task_key, "raw": body, "callback_url": None}, specs)
            return {**accepted, "owner": {"sourceSystem": source, "mineCode": mine, "environment": environment},
                    "debug_task": {**body, "callbackMode": "none"}}
        request = {
            "sourceSystem": source, "mineCode": mine, "checkCycle": cycle, "environment": environment,
            "scope": {"manifest": str(Path(manifest).resolve())}, "callbackMode": "none",
            "items": [{"itemCode": item["item_code"]} for item in items],
        }
        if body.get("ruleVersion") is not None:
            request["ruleVersion"] = body["ruleVersion"]
        from .sources import LocalDocumentSource
        try:
            content = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise InvalidRequest("manifestPath must contain readable JSON") from exc
        try:
            LocalDocumentSource._validate_manifest(content, {"source_system": source, "mine_code": mine, "environment": environment})
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(str(exc)) from exc
        specs = parse_batch(request, inspection_config=config)
        from dataclasses import replace
        specs = [replace(spec, task_key=task_key) for spec in specs]
        accepted = self.ledger.accept_task({"task_key": task_key, "raw": body, "callback_url": None}, specs)
        return {"items": accepted["items"], "debug_task": {
            "mineId": mine, "status": "running", "checkCycle": cycle,
            "items": items, "manifestPath": str(Path(manifest).resolve()),
            "resultCallbackUrl": body.get("resultCallbackUrl"), "callbackMode": "none",
        }, "task_key": task_key, "owner": {"sourceSystem": source, "mineCode": mine, "environment": environment}}

    def run_once(self, worker_id="worker", *, lease_seconds=30, run_id=None, debug_only=False):
        run = self.ledger.claim(worker_id, lease_seconds=lease_seconds, run_id=run_id, debug_only=debug_only)
        if run is None:
            return None
        if "manifest" in run["spec"]["scope"] or "oceanbase" in run["spec"]["scope"]:
            with self._renewal(run, worker_id, lease_seconds):
                return self._execute(run, worker_id)
        return self._execute(run, worker_id)

    def execute_debug_items(self, items):
        for item in items:
            try:
                self.run_once("debug-" + item["run_id"], run_id=item["run_id"])
            except StaleLease:
                continue

    @contextmanager
    def _renewal(self, run, worker_id, lease_seconds):
        stop = Event()
        def heartbeat():
            while not stop.wait(max(0.01, lease_seconds / 3)):
                try:
                    self.ledger.renew(run["run_id"], worker_id, run["fence"], lease_seconds)
                except Exception:
                    # Main-thread writes still require the current, unexpired lease.
                    return
        thread = Thread(target=heartbeat, name="document-lease-renewal", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=6)

    def _execute(self, run, worker_id):
        self.fault_hook("after_claim")
        snapshot = run.get("snapshot")
        saved_snapshot = snapshot
        stage = "source"
        try:
            self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                        {"stage": "run", "event": "claimed", "status": "running"})
            if run.get("execution_attempt", run["fence"]) > self.max_attempts:
                raise RuntimeError("execution recovery attempt limit exceeded")
            if snapshot is None:
                started = perf_counter()
                self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                            {"stage": "source", "event": "started", "status": "running"})
                snapshot = self.source(run)
                if run["spec"].get("inspection_item"):
                    snapshot["inspection_configuration"] = run["spec"]["inspection_item"]
                self.ledger.save_snapshot(run["run_id"], worker_id, run["fence"], snapshot)
                saved_snapshot = snapshot
                self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                            {"stage": "source", "event": "finished", "status": "completed",
                                             "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
            self.fault_hook("after_snapshot")
            stage = "evaluation"
            started = perf_counter()
            self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                        {"stage": "evaluation", "event": "started", "status": "running"})
            result = self.evaluator(run, snapshot)
            self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                        {"stage": "evaluation", "event": "finished", "status": result["status"],
                                         "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
            stage = "result_validation"
            self._finish(run, worker_id, result, saved_snapshot)
        except RunPaused:
            self.ledger.pause_run(run["run_id"], worker_id, run["fence"])
            return {"run_id": run["run_id"], "execution_state": "paused"}
        except StaleLease:
            # A worker without the current lease must never publish a failure either.
            raise
        except Exception as exc:
            # Terminate a poison item without losing the rest of the batch. Actual
            # process death (BaseException) still recovers through the durable lease.
            remote = "oceanbase" in run["spec"]["scope"]
            error_detail = type(exc).__name__ if remote else f"{type(exc).__name__}: {exc}"
            result = result_base(run, "failed", "检查执行技术失败", error_detail)
            if "manifest" in run["spec"]["scope"] or "oceanbase" in run["spec"]["scope"]:
                result["profile"] = "local-documents"
            result.update(error_type=type(exc).__name__, error_stage=stage, execution_attempt=run["fence"])
            if saved_snapshot is not None:
                result["snapshot_id"] = saved_snapshot["snapshot_id"]
            try:
                self.ledger.record_progress(run["run_id"], worker_id, run["fence"],
                                            {"stage": stage, "event": "failed", "status": "failed",
                                             "issues": [type(exc).__name__]})
                self._finish(run, worker_id, result, saved_snapshot)
            except RunPaused:
                self.ledger.pause_run(run["run_id"], worker_id, run["fence"])
                return {"run_id": run["run_id"], "execution_state": "paused"}
        self.fault_hook("after_finish")
        return result

    def _finish(self, run, worker_id, result, snapshot):
        payload = None
        if run["spec"]["callback_mode"] == "real":
            from .callbacks import map_callback
            finalized = datetime.fromtimestamp(self.ledger.clock(), timezone.utc).isoformat()
            payload = map_callback(run, result, snapshot, finalized)
        self.ledger.finish(run["run_id"], worker_id, run["fence"], result, delivery_payload=payload)

    def deliver_once(self, worker_id="delivery", *, lease_seconds=30, receiver=None):
        delivery = self.ledger.claim_delivery(worker_id, lease_seconds=lease_seconds)
        if delivery is None:
            return None
        self.fault_hook("after_delivery_claim")
        from .callbacks import CallbackPermanentError, CallbackRetryableError
        receive = receiver or self.ledger.record_mock_delivery
        started = perf_counter()
        try:
            if delivery.get("callback_url"):
                if self.callback_client is None:
                    raise CallbackPermanentError("HTTP callback client is not configured")
                if getattr(self.callback_client, "timeout", 10) >= lease_seconds:
                    raise CallbackPermanentError("callback timeout must be shorter than delivery lease")
                self.callback_client.send(delivery["callback_url"], delivery["payload"], delivery["delivery_id"])
            else:
                receive(delivery["delivery_id"], delivery["payload"])
        except (CallbackPermanentError, CallbackRetryableError, TimeoutError, ConnectionError, OSError) as exc:
            permanent = isinstance(exc, CallbackPermanentError) or delivery["attempt"] >= self.max_attempts
            self.ledger.complete_delivery(delivery["delivery_id"], worker_id, delivery["fence"], error=str(exc)[:512], permanent=permanent,
                                          elapsed_ms=(perf_counter()-started)*1000)
            return {"delivery_id": delivery["delivery_id"], "state": "failed" if permanent else "waiting"}
        self.fault_hook("after_mock_receive")
        self.ledger.complete_delivery(delivery["delivery_id"], worker_id, delivery["fence"], elapsed_ms=(perf_counter()-started)*1000)
        return {"delivery_id": delivery["delivery_id"], "state": "delivered"}
