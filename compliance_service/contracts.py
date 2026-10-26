"""Small, shared contracts; no legacy project imports or external calls."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


RESULT_CODES = frozenset({"pass", "no_pass", "pending", "unverifiable", "failed"})
TERMINAL_STATES = frozenset({"completed", "failed"})
DOCUMENT_DEFINITION_VERSION = "statutory-2026-v1"
DEFINITIONS = {
    "stop_work_unload": {"version": "engineering-mock-v1", "name": "停工撤人与卸压",
                         "required_propositions": ("trigger_handled", "release_verified")},
    "relief_effects": {"version": "engineering-mock-v1", "name": "解危效果检验",
                      "required_propositions": ("required_measurements", "below_threshold", "release_authorized")},
}


class InvalidRequest(ValueError):
    pass


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class StaleLease(Conflict):
    pass


class RunPaused(Exception):
    """Cooperative control signal, never a business failure."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise InvalidRequest(f"{name} must be a non-empty string of at most 256 characters")
    return value  # Preserve caller's checkCycle exactly, including surrounding spaces.


def default_check_cycle() -> str:
    """Return the current task cycle in the business timezone.

    Upstream callers may omit checkCycle during local probing. Once accepted,
    the resolved cycle is persisted in the run spec so the fallback cannot
    drift while a long task is executing.
    """
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")


def resolve_check_cycle(value: Any) -> Any:
    """Fallback only when absent/null; malformed supplied values stay invalid."""
    return default_check_cycle() if value is None else value


@dataclass(frozen=True)
class RunSpec:
    source_system: str
    mine_code: str
    item_code: str
    check_cycle: str
    scope: dict
    environment: str = "local"
    callback_mode: str = "mock"
    definition_version: str | None = None
    rulebook_version: str | None = None
    rulebook_revision: str | None = None
    callback_url: str | None = None
    task_key: str | None = None
    inspection_item: dict | None = None

    def __post_init__(self):
        if not isinstance(self.scope, dict) or set(self.scope) not in ({"fixture"}, {"manifest"}, {"oceanbase"}):
            raise InvalidRequest("explicit fixture or local manifest scope required")
        remote = "oceanbase" in self.scope
        real_documents = "manifest" in self.scope or remote
        expected_version = DOCUMENT_DEFINITION_VERSION if real_documents else "engineering-mock-v1"
        if self.definition_version is None:
            object.__setattr__(self, "definition_version", expected_version)
        for field in ("source_system", "mine_code", "item_code", "check_cycle", "environment", "callback_mode", "definition_version"):
            required_text(getattr(self, field), field)
        item = self.inspection_item
        if item is not None:
            from copy import deepcopy
            if not isinstance(item, dict) or item.get("itemCode") != self.item_code or item.get("handler") not in DEFINITIONS or item.get("enabled") is not True:
                raise InvalidRequest("invalid accepted inspection configuration")
            object.__setattr__(self, "inspection_item", deepcopy(item))
        elif self.item_code not in DEFINITIONS:
            raise InvalidRequest("unknown itemCode")
        if self.definition_version != expected_version:
            raise InvalidRequest("unsupported definition version")
        if self.environment not in {"local", "test", "production"}:
            raise InvalidRequest("only local/test environments are available")
        if self.callback_mode not in {"mock", "none", "real"}:
            raise InvalidRequest("real callbacks are not configured")
        if self.callback_mode == "real" and not (remote and self.callback_url and self.task_key):
            raise InvalidRequest("real callbacks require a trusted upstream task")
        if remote and (not self.task_key or self.scope["oceanbase"] != self.task_key):
            raise InvalidRequest("OceanBase scope must identify its accepted task")
        if real_documents:
            from .rulebook import DEFAULT_RULEBOOK_VERSION, load_rulebook
            requested_rulebook = self.rulebook_version or DEFAULT_RULEBOOK_VERSION
            try:
                baseline = load_rulebook(requested_rulebook)
            except (ValueError, TypeError) as exc:
                raise InvalidRequest(str(exc)) from exc
            if self.rulebook_revision not in (None, baseline["revision"]):
                raise InvalidRequest("rulebook revision is not the registered immutable version")
            object.__setattr__(self, "rulebook_version", requested_rulebook)
            object.__setattr__(self, "rulebook_revision", baseline["revision"])
            if not remote:
                path = self.scope["manifest"]
                if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
                    raise InvalidRequest("manifest must be an absolute local JSON path")
                object.__setattr__(self, "scope", {"manifest": str(Path(path).resolve())})
        else:
            if self.rulebook_version is not None or self.rulebook_revision is not None:
                raise InvalidRequest("fixture mode does not use a document rulebook")
            required_text(self.scope["fixture"], "scope.fixture")
            if self.scope["fixture"] not in {"clean", "violation", "missing", "unreadable", "source_error"}:
                raise InvalidRequest("unknown fixture")
        canonical(self.scope)

    @property
    def scope_key(self) -> str:
        # Request identity is available before source discovery. No byte hash here.
        value = self.to_dict()
        value.pop("callback_mode")
        return digest(value)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def handler_code(self) -> str:
        return (self.inspection_item or {}).get("handler", self.item_code)


def parse_batch(body: Any, *, inspection_config: dict | None = None) -> list[RunSpec]:
    """Validate the request before accepting any items (per-item execution is isolated)."""
    if not isinstance(body, dict):
        raise InvalidRequest("request must be an object")
    scope_value = body.get("scope")
    if body.get("callbackMode") == "real" or (isinstance(scope_value, dict) and "oceanbase" in scope_value):
        raise InvalidRequest("use the authenticated inspection task entry for network acquisition")
    allowed = {"sourceSystem", "mineCode", "checkCycle", "scope", "items", "environment", "callbackMode", "ruleVersion"}
    if set(body) - allowed:
        raise InvalidRequest("unknown request fields")
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise InvalidRequest("items must contain between 1 and 100 entries")
    specs = []
    check_cycle = resolve_check_cycle(body.get("checkCycle"))
    for item in items:
        if not isinstance(item, dict) or not set(item) <= {"itemCode", "scope", "ruleVersion"}:
            raise InvalidRequest("invalid item fields")
        specs.append(RunSpec(
            source_system=body.get("sourceSystem"), mine_code=body.get("mineCode"),
            item_code=item.get("itemCode"), check_cycle=check_cycle,
            scope=item.get("scope", body.get("scope")),
            environment=body.get("environment", "local"),
            callback_mode=body.get("callbackMode", "mock"),
            rulebook_version=item.get("ruleVersion", body.get("ruleVersion")),
            inspection_item=accepted_inspection_item(inspection_config, item.get("itemCode"), check_cycle) if inspection_config is not None else None,
        ))
    keys = [spec.scope_key for spec in specs]
    if len(keys) != len(set(keys)):
        raise InvalidRequest("duplicate item scopes in one request")
    return specs


def rule_handler(spec: dict) -> str:
    return (spec.get("inspection_item") or {}).get("handler", spec["item_code"])


def accepted_inspection_item(config: dict, code: str, check_cycle: str) -> dict:
    from .inspection_config import find_item, revision
    check_cycle = resolve_check_cycle(check_cycle)
    item = find_item(config, code)
    # New admissions always resolve the actual month from the task cycle.
    # Older accepted snapshots retain their frozen policy and resolved month.
    policy = {"month_source": "task_month"}
    month = str(check_cycle)[5:7]
    item.update(query_policy=policy, revision=revision(config), resolved_query_month=month)
    return item


def assert_snapshot_owner(snapshot: dict, run: dict) -> None:
    spec = run["spec"]
    if spec.get("rulebook_version"):
        baseline = snapshot.get("rule_baseline") or {}
        if (baseline.get("version"), baseline.get("revision")) != (spec["rulebook_version"], spec["rulebook_revision"]):
            raise InvalidRequest("snapshot rulebook differs from accepted rule version")
    for name, expected in {
        "run_id": run["run_id"], "mine_code": spec["mine_code"],
        "source_system": spec["source_system"], "environment": spec["environment"],
    }.items():
        if snapshot.get(name) != expected:
            raise InvalidRequest(f"snapshot ownership mismatch: {name}")
    snapshot_id = snapshot.get("snapshot_id")
    files = snapshot.get("files")
    if not isinstance(snapshot_id, str) or not isinstance(files, list):
        raise InvalidRequest("invalid input snapshot")
    evidence_ids = set()
    for evidence in snapshot.get("evidence", []):
        if not isinstance(evidence, dict):
            raise InvalidRequest("invalid evidence")
        for name in ("run_id", "mine_code", "source_system", "environment"):
            if evidence.get(name) != snapshot[name]:
                raise InvalidRequest(f"evidence ownership mismatch: {name}")
        if evidence.get("snapshot_id") != snapshot_id:
            raise InvalidRequest("evidence snapshot mismatch")
        if evidence.get("content_hash") not in {f.get("content_hash") for f in files}:
            raise InvalidRequest("evidence file is not in input snapshot")
        eid = evidence.get("evidence_id")
        if not isinstance(eid, str) or eid in evidence_ids:
            raise InvalidRequest("invalid or duplicate evidence ID")
        evidence_ids.add(eid)
    for fact in snapshot.get("facts", []):
        if not isinstance(fact, dict) or not fact.get("evidence_refs"):
            raise InvalidRequest("fact must reference registered evidence")
        if not set(fact["evidence_refs"]) <= evidence_ids:
            raise InvalidRequest("fact references unregistered evidence")
        if snapshot.get("profile") == "local-documents":
            evidence_by_id = {e["evidence_id"]: e for e in snapshot["evidence"]}
            if any(evidence_by_id[ref].get("source_role") != fact.get("source_role") for ref in fact["evidence_refs"]):
                raise InvalidRequest("fact source role does not match cited evidence")
    fact_ids = {f["fact_id"] for f in snapshot.get("facts", [])}
    for link in snapshot.get("association_links", []):
        if not isinstance(link, dict) or link.get("fact_id") not in fact_ids:
            raise InvalidRequest("association link references an unregistered fact")
        if not link.get("evidence_refs") or not set(link["evidence_refs"]) <= evidence_ids:
            raise InvalidRequest("association link references unregistered evidence")


def validate_result(result: dict, run: dict, snapshot: dict | None) -> None:
    spec = run["spec"]
    if result.get("status") not in RESULT_CODES:
        raise InvalidRequest("invalid result code")
    for name, expected in {
        "run_id": run["run_id"], "mine_code": spec["mine_code"],
        "itemCode": spec["item_code"], "checkCycle": spec["check_cycle"],
        "definition_version": spec["definition_version"],
    }.items():
        if result.get(name) != expected:
            raise InvalidRequest(f"result mismatch: {name}")
    for field in ("description", "detail"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise InvalidRequest(f"{field} is required")
    if result["status"] in {"pass", "no_pass"} and not result.get("evidence_refs"):
        raise InvalidRequest("a conclusive business result requires cited evidence")
    if snapshot is None:
        if result["status"] != "failed" or result.get("evidence_refs"):
            raise InvalidRequest("only a technical failure may finish without a snapshot")
    else:
        assert_snapshot_owner(snapshot, run)
        if result.get("snapshot_id") != snapshot["snapshot_id"]:
            raise InvalidRequest("result snapshot mismatch")
        registered = {e["evidence_id"] for e in snapshot["evidence"]}
        if not set(result.get("evidence_refs", [])) <= registered:
            raise InvalidRequest("unregistered evidence reference")
        if result["status"] == "pass" and snapshot.get("source_complete") is not True:
            raise InvalidRequest("incomplete source cannot pass")
