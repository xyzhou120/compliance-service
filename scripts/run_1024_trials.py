"""Repeat the three selected real-document rounds with enabled local providers.

Only the private attachment host listed in the supplied inventory is downloaded.
Its previously observed certificate is pinned for this isolated local fixture;
the service's normal TLS validation is unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import sys
from time import perf_counter
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = ROOT / ".local/validation/1024-multi-round"
NAMES = ("round-0923-0925-effective", "round-1014-1016-existing", "round-1020-1022-latest")
CERTIFICATE = "a18961047de19bbb48a803950b3c5c4817969fa3d3669e7980892e97c198c80d"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def checked_file(path, suffix):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 50 * 1024 * 1024:
        return False
    with path.open("rb") as stream:
        magic = stream.read(8)
    return magic.startswith(b"%PDF-") if suffix == ".pdf" else magic.startswith(b"PK")


def download(url, path):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "172.26.10.67" or parts.port != 8082:
        raise ValueError("attachment destination differs from the selected inventory host")
    endpoint = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        endpoint += "?" + quote(parts.query, safe="%=&/:?@!$'()*+,;+-._~")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(parts.hostname, parts.port, context=context, timeout=40)
    partial = Path(str(path) + ".part")
    try:
        connection.connect()
        peer_hash = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if peer_hash != CERTIFICATE:
            raise ValueError("attachment certificate differs from the recorded fingerprint")
        connection.request("GET", endpoint)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"attachment HTTP {response.status}; redirects are not followed")
        size = 0
        with partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise ValueError("attachment exceeds 50 MiB")
                output.write(chunk)
        if not checked_file(partial, path.suffix.lower()):
            raise ValueError("attachment format signature does not match")
        partial.replace(path)
    finally:
        connection.close()
        partial.unlink(missing_ok=True)


def prepare(name):
    request = read_json(BASE / f"{name}.request.json")
    directory = BASE / name
    source = directory / "source"
    source.mkdir(parents=True, exist_ok=True)
    # Match by attachment ID, never zip a partially successful download list.
    cached = {}
    for manifest in (directory / "download-provenance.json",):
        if manifest.exists():
            for record in read_json(manifest):
                item = record.get("item") or {}
                identity = (item.get("source_metadata") or {}).get("attachment_id")
                if identity:
                    cached[identity] = record.get("local_path")
    records, missing = [], []
    for index, item in enumerate(request["attachments"], 1):
        identity = item["source_metadata"]["attachment_id"]
        suffix = Path(item["name"]).suffix.lower()
        original = item.get("path") or cached.get(identity)
        path = Path(original) if original else source / f"{identity}{suffix}"
        if not checked_file(path, suffix):
            path = source / f"{identity}{suffix}"
            for attempt in range(2):
                try:
                    download(item["url"], path)
                    break
                except Exception as exc:
                    print(json.dumps({"round": name, "attachment": identity, "attempt": attempt + 1,
                                      "error": type(exc).__name__}, ensure_ascii=False), flush=True)
            if not checked_file(path, suffix):
                missing.append(identity)
                continue
        item.pop("url", None)
        item.update(kind="local_path", path=str(path.resolve()))
        records.append({"attachment_id": identity, "original_name": item["name"], "path": str(path.resolve()),
                        "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json(directory / "verified-attachments.json", records)
    write_json(directory / "missing-attachments.json", missing)
    if missing:
        raise RuntimeError(f"{name}: {len(missing)} attachments still unavailable; no task submitted")
    write_json(directory / "verified-request.json", request)
    return request, directory


def run(name):
    from fastapi.testclient import TestClient
    from compliance_service.api import create_app
    from compliance_service.config import load_model_settings
    from compliance_service.ledger import Ledger
    from compliance_service.service import InspectionService
    from compliance_service.sources import LocalDocumentSource

    request, directory = prepare(name)
    settings = load_model_settings(ROOT / "config/models.local.toml")
    if not all(settings[kind]["enabled"] for kind in ("ocr", "vision")):
        raise RuntimeError("real trials require both configured providers to be enabled")
    # Daily reports are four pages and analyses two pages. Do not pull large
    # annual reports into every round or silently switch off either provider.
    settings["ocr"].update(max_pages=4, timeout_seconds=15)
    settings["vision"].update(timeout_seconds=45, max_output_tokens=8192)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ledger = Ledger(directory / f"trial-{stamp}.sqlite3")
    source = LocalDocumentSource(BASE / "artifacts", model_settings=settings)

    def source_with_progress(run):
        def progress(event):
            ledger.record_progress(run["run_id"], run["worker_id"], run["fence"], event)
            if event.get("event") in {"file_started", "file_finished", "request_finished", "source_finished"}:
                print(json.dumps({"round": name, **event}, ensure_ascii=False), flush=True)
        return source(run, progress_callback=progress)

    service = InspectionService(ledger, source=source_with_progress)
    started = perf_counter()
    with TestClient(create_app(service)) as client:
        response = client.post("/v1/debug/bundles", json=request)
        if response.status_code != 202:
            raise RuntimeError(f"submission HTTP {response.status_code}: {response.text}")
        accepted = response.json()
        write_json(directory / "trial-accepted.json", accepted)
        service.run_once()
        run_id = accepted["items"][0]["run_id"]
        result = client.get(f"/v1/runs/{run_id}", params={
            "source_system": request["sourceSystem"], "mine_code": request["mineCode"],
            "environment": request["environment"],
        }).json()
        events, after = [], 0
        while True:
            progress = ledger.get_progress(run_id, request["sourceSystem"], request["mineCode"],
                                           request["environment"], after=after, limit=500)
            events.extend(progress["events"])
            after = progress["next_after"]
            if not progress["has_more"]:
                break
    write_json(directory / "trial-result.json", {"accepted": accepted, "run": result, "progress": events,
        "validation": {"ocr_enabled": True, "vision_enabled": True, "max_pages_per_document": 4,
                       "elapsed_seconds": round(perf_counter() - started, 3), "ledger": str(ledger.path)}})
    print(json.dumps({"round": name, "run_id": run_id, "status": result.get("result", {}).get("status"),
                      "daily_summary": result.get("result", {}).get("daily_summary")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", choices=NAMES + ("all",), default="all")
    args = parser.parse_args()
    failures = []
    for name in NAMES if args.round == "all" else (args.round,):
        try:
            run(name)
        except Exception as exc:
            failures.append({"round": name, "error": type(exc).__name__, "detail": str(exc)})
            write_json(BASE / name / "trial-failure.json", failures[-1])
            print(json.dumps(failures[-1], ensure_ascii=False), flush=True)
    write_json(BASE / "trial-failures.json", failures)
    raise SystemExit(bool(failures))
