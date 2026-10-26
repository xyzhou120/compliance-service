"""Synthetic source and proposition provider. This does not parse real documents."""

from hashlib import sha256

from .contracts import DEFINITIONS, InvalidRequest, canonical, digest, rule_handler


class SourceFailure(RuntimeError):
    pass


def load_snapshot(run: dict) -> dict:
    spec = run["spec"]
    scenario = spec["scope"]["fixture"]
    if scenario == "source_error":
        raise SourceFailure("Synthetic source unavailable; no real connection was attempted")
    ownership = {"run_id": run["run_id"], **{
        name: spec[name] for name in ("mine_code", "source_system", "environment")
    }}
    required = DEFINITIONS[rule_handler(spec)]["required_propositions"]
    text = canonical({"synthetic": True, "scenario": scenario, "necessary_conditions": required})
    content_hash = sha256(text.encode("utf-8")).hexdigest()
    files = [{"file_id": "synthetic-source", "content_hash": content_hash, "format": "fixture-text"}]
    snapshot_id = digest({**ownership, "files": files})
    evidence_id = digest({"snapshot_id": snapshot_id, "block": 1})
    evidence = [{**ownership, "snapshot_id": snapshot_id, "evidence_id": evidence_id,
                 "content_hash": content_hash, "text": text,
                 "locator": {"kind": "fixture-block", "block": 1}}]
    propositions = [{"code": code, "state": "true", "evidence_refs": [evidence_id]} for code in required]
    if scenario == "violation":
        propositions[0]["state"] = "false"
    elif scenario == "missing":
        propositions[-1].update(state="missing", evidence_refs=[])
    elif scenario == "unreadable":
        propositions[-1].update(state="unreadable", evidence_refs=[])
    return {
        **ownership, "snapshot_id": snapshot_id, "profile": "engineering-fixture",
        "parser_version": "fixture-parser-v1", "source_complete": scenario != "missing",
        "files": files, "evidence": evidence, "propositions": propositions,
        "coverage_gaps": ["necessary material missing"] if scenario == "missing" else [],
    }


def evaluate(run: dict, snapshot: dict) -> dict:
    """Fixed mock policy for testing orchestration; never a statutory rule package."""
    from .contracts import assert_snapshot_owner
    assert_snapshot_owner(snapshot, run)
    propositions = snapshot.get("propositions", [])
    required = set(DEFINITIONS[rule_handler(run["spec"])]["required_propositions"])
    codes = [p.get("code") for p in propositions]
    if len(set(codes)) != len(codes) or not set(codes) <= required:
        raise InvalidRequest("duplicate or unknown necessary proposition")
    missing = required - set(codes)
    registered = {entry["evidence_id"] for entry in snapshot["evidence"]}
    if any(not set(p.get("evidence_refs", [])) <= registered for p in propositions):
        raise ValueError("fixture proposition references unregistered evidence")
    proven = [p for p in propositions if p.get("evidence_refs")]
    if any(p["state"] == "false" for p in proven):
        code, description = "no_pass", "合成证据证明必要条件不满足"
    elif any(p["state"] == "unreadable" for p in propositions):
        code, description = "unverifiable", "必要内容不可读，无法判定"
    elif (snapshot["source_complete"] is not True or missing or not propositions
          or any(p["state"] != "true" or not p.get("evidence_refs") for p in propositions)):
        code, description = "pending", "资料或证据不足，本轮结束"
    else:
        code, description = "pass", "合成必要命题均满足"
    result = result_base(run, code, description, "工程模拟结果；真实文档和法规准确性尚未验证")
    result.update(
        snapshot_id=snapshot["snapshot_id"],
        evidence_refs=sorted({ref for p in proven for ref in p["evidence_refs"]}),
        coverage_gaps=snapshot.get("coverage_gaps", []) + [f"missing proposition: {code}" for code in sorted(missing)],
        propositions=propositions,
    )
    if code == "no_pass":
        result["attachments"] = [{"file_id": snapshot["files"][0]["file_id"],
                                  "attachmentContent": description}]
    else:
        # No attachmentContent: empty string and null have different upstream meaning.
        result["attachments"] = [{"file_id": f["file_id"]} for f in snapshot["files"]]
    return result


def result_base(run: dict, status: str, description: str, detail: str) -> dict:
    spec = run["spec"]
    return {
        "run_id": run["run_id"], "mine_code": spec["mine_code"],
        "itemCode": spec["item_code"], "checkCycle": spec["check_cycle"],
        "definition_version": spec["definition_version"], "status": status,
        "description": description, "detail": detail,
        "profile": "engineering-fixture", "evidence_refs": [],
    }
