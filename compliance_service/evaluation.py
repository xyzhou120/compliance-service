"""Offline, explicit-denominator accuracy reports; never edit an inspection result."""

from collections import Counter
import json
from pathlib import Path

from .contracts import RESULT_CODES, InvalidRequest


def summarize_cases(cases: list[dict]) -> dict:
    seen, confusion, rows = set(), Counter(), []
    for case in cases:
        case_id = case.get("case_id")
        expected = case.get("expected_status")
        actual = case.get("result", {}).get("status")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise InvalidRequest("case IDs must be unique and non-empty")
        if expected not in RESULT_CODES or actual not in RESULT_CODES:
            raise InvalidRequest("each evaluation requires an explicit valid expected and actual status")
        origin = case.get("origin")
        if origin not in {"synthetic_document", "real_mine_gold"}:
            raise InvalidRequest("evaluation origin must distinguish synthetic documents from real mine gold")
        seen.add(case_id)
        confusion[expected, actual] += 1
        snapshot = case.get("snapshot")
        refs = case["result"].get("evidence_refs", [])
        registered = {e["evidence_id"] for e in snapshot.get("evidence", [])} if snapshot else set()
        traceable = all(ref in registered for ref in refs)
        rows.append({"case_id": case_id, "origin": origin, "expected": expected, "actual": actual,
                     "correct": expected == actual, "evidence_traceable": traceable,
                     "coverage_gap_count": len(case["result"].get("coverage_gaps", []))})
    total = len(rows)
    determined = sum(row["actual"] in {"pass", "no_pass"} for row in rows)
    business_cases = [row for row in rows if row["expected"] in {"pass", "no_pass"}]
    return {
        "case_count": total, "origin_counts": dict(Counter(row["origin"] for row in rows)),
        "label_accuracy": sum(row["correct"] for row in rows) / total if total else None,
        "business_decision_denominator": len(business_cases),
        "business_decision_accuracy": (sum(row["correct"] for row in business_cases) / len(business_cases)
                                       if business_cases else None),
        "determinate_rate": determined / total if total else None,
        "unsafe_pass_count": sum(row["actual"] == "pass" and row["expected"] != "pass" for row in rows),
        "missed_violation_count": sum(row["expected"] == "no_pass" and row["actual"] != "no_pass" for row in rows),
        "untraceable_case_count": sum(not row["evidence_traceable"] for row in rows),
        "confusion": [{"expected": expected, "actual": actual, "count": count}
                      for (expected, actual), count in sorted(confusion.items())],
        "production_accuracy_validated": False,
        "limitation": "Case results describe the supplied gold set only; representative mine coverage and final acceptance remain separate.",
        "cases": rows,
    }


def run_evaluation(case_file, output_dir) -> dict:
    """Run a user-supplied, explicitly labelled local gold set in isolated ledgers."""
    from .contracts import digest
    from .ledger import Ledger
    from .service import InspectionService

    case_file, output_dir = Path(case_file).resolve(), Path(output_dir).resolve()
    specs = json.loads(case_file.read_text(encoding="utf-8-sig"))
    if not isinstance(specs, list):
        raise InvalidRequest("evaluation cases must be a JSON list")
    # Validate labels up front; none may be inferred from the implementation output.
    summarize_cases([{"case_id": spec.get("case_id"), "expected_status": spec.get("expected_status"),
                      "origin": spec.get("origin"), "result": {"status": "pending"}} for spec in specs])
    rows, runs = [], []
    for number, spec in enumerate(specs):
        manifest_path = (case_file.parent / spec["manifest"]).resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        ledger_path = output_dir / "runs" / f"{number:04d}-{digest(spec['case_id'])[:12]}" / "ledger.sqlite3"
        ledger = Ledger(ledger_path)
        service = InspectionService(ledger)
        body = {"sourceSystem": manifest["source_system"], "mineCode": manifest["mine_code"],
                "environment": manifest["environment"], "checkCycle": spec["checkCycle"],
                "scope": {"manifest": str(manifest_path)}, "callbackMode": "none",
                "items": [{"itemCode": spec["itemCode"]}]}
        accepted = service.submit(body)["items"][0]
        while service.run_once() is not None:
            pass
        run = ledger.get(accepted["run_id"], manifest["source_system"], manifest["mine_code"], manifest["environment"])
        rows.append({**spec, "result": run["result"], "snapshot": run["snapshot"]})
        runs.append({"case_id": spec["case_id"], "run_id": run["run_id"], "ledger": str(ledger_path)})
    report = summarize_cases(rows)
    report["runs"] = runs
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "accuracy.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
