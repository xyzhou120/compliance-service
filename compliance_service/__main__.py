"""Local entry points. Importing the package does not create files or connect."""

import argparse
import json
import os
import time
from pathlib import Path

from .contracts import StaleLease


def main():
    parser = argparse.ArgumentParser(description="Independent compliance service — M0/M1 fixture profile")
    parser.add_argument("--db", default=".local/service.sqlite3")
    parser.add_argument("--production-config", help="opt-in TOML for upstream/OB/callback adapters")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    samples = sub.add_parser("make-samples", help="write reproducible full-check acceptance files")
    samples.add_argument("--output", default=".local/samples")
    demo = sub.add_parser("demo")
    demo.add_argument("--scenario", choices=["clean", "violation", "missing", "unreadable", "source_error"], default="clean")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--debug-only", action="store_true", help="claim local simulator tasks only")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    inspect = sub.add_parser("check-files", help="one-shot offline inspection; does not start an HTTP server")
    inspect.add_argument("--manifest", required=True)
    inspect.add_argument("--check-cycle", required=True)
    inspect.add_argument("--item", choices=["both", "stop_work_unload", "relief_effects"], default="both")
    inspect.add_argument("--report")
    evaluation = sub.add_parser("evaluate-files", help="one-shot labelled local cases; no service startup")
    evaluation.add_argument("--cases", required=True)
    evaluation.add_argument("--output", default=".local/accuracy")
    args = parser.parse_args()
    if args.command == "make-samples":
        from .samples import create_validation_samples
        print(json.dumps(create_validation_samples(args.output), ensure_ascii=False, indent=2))
        return
    if args.command == "evaluate-files":
        from .evaluation import run_evaluation
        print(json.dumps(run_evaluation(args.cases, args.output), ensure_ascii=False, indent=2))
        return
    from .runtime import build_service
    service = build_service(Path(args.db), args.production_config)
    if args.command == "init":
        print(json.dumps({"database": str(Path(args.db).resolve()),
                          "profile": "upstream-configured" if args.production_config else "local-fixture",
                          "real_connectivity": False}))
    elif args.command == "demo":
        accepted = service.submit({
            "sourceSystem": "demo", "mineCode": "synthetic-mine", "checkCycle": "sample-2026-10",
            "scope": {"fixture": args.scenario}, "items": [{"itemCode": "stop_work_unload"}, {"itemCode": "relief_effects"}],
        })
        while service.run_once() is not None:
            pass
        while service.deliver_once() is not None:
            pass
        print(json.dumps({"accepted": accepted, "runs": [service.ledger.get(
            entry["run_id"], "demo", "synthetic-mine", "local"
        ) for entry in accepted["items"]]}, ensure_ascii=False, indent=2))
    elif args.command == "check-files":
        path = Path(args.manifest).resolve()
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        items = ["stop_work_unload", "relief_effects"] if args.item == "both" else [args.item]
        accepted = service.submit({
            "sourceSystem": manifest["source_system"], "mineCode": manifest["mine_code"],
            "environment": manifest["environment"], "checkCycle": args.check_cycle,
            "scope": {"manifest": str(path)}, "callbackMode": "none",
            "items": [{"itemCode": item} for item in items],
        })
        while service.run_once() is not None:
            pass
        report = {"items": [service.ledger.get(item["run_id"], manifest["source_system"],
                                              manifest["mine_code"], manifest["environment"])
                            for item in accepted["items"]]}
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            output = Path(args.report)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        print(text)
    elif args.command == "worker":
        if args.debug_only and (not getattr(service, "local_debug", False) and args.production_config):
            raise SystemExit("debug-only worker requires local debug configuration")
        while True:
            try:
                execution = service.run_once(debug_only=args.debug_only)
                delivery = None if args.debug_only or getattr(service, "local_debug", False) else service.deliver_once()
            except StaleLease:
                # A different process owns this run now; leave its writes alone.
                execution = delivery = None
            if args.once:
                break
            if execution is None and delivery is None:
                time.sleep(0.2)
    else:
        import uvicorn
        from .api import create_app
        service.external_worker = os.environ.get("COMPLIANCE_DEBUG_EXTERNAL_WORKER") == "1"
        if not getattr(service, "task_token", None) and args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("local configuration and debug routes only allow a loopback bind address")
        uvicorn.run(create_app(service), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
