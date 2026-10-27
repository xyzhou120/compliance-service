"""Offline local-file acquisition, immutable copies and provenance-bound evidence."""

from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from importlib.metadata import version
from importlib.resources import files as package_files
import os
import gc
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

from .contracts import InvalidRequest, RunPaused, StaleLease, canonical, digest
from .parsers import parse_document


PARSER_VERSION = "native-documents-v1"
ROLES = {"actual_record", "requirement", "statement", "regulation"}
STOP_WORK_ORDER = {
    "prevention_daily": 0, "threshold": 1, "analysis": 2,
    "dispatch_daily": 3, "release": 4, "relief_acceptance": 5,
    "design": 6, "workface_risk": 7, "coal_risk": 8,
}


def _io_path(path: Path) -> Path:
    """Use extended Windows paths for hash-addressed storage in deep workspaces."""
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    return Path(text)


def _atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _has_failed_recognition(parsed):
    """Provider errors may be retried; successful provider candidates live separately."""
    for unit in parsed.get("coverage", []):
        recognition = unit.get("recognition") or {}
        if recognition.get("status") == "failed":
            return True
        for image in recognition.get("images", []):
            if any(candidate.get("issues") for candidate in image.get("recognition_candidates", [])):
                return True
    return False


def _page_receipts(parsed_files):
    """Per-page receipts are opt-in (COMPLIANCE_PAGE_RECEIPTS=1).

    Default off keeps the legacy snapshot shape and behaviour untouched.
    """
    from . import pipeline_flags
    from .ingest import build_page_receipts
    if not (pipeline_flags.enabled("page_receipts") or pipeline_flags.snapshot().get("page_receipts")):
        return []
    try:
        return build_page_receipts(parsed_files)
    except Exception:                       # receipts must never fail a run
        return []


class LocalDocumentSource:
    """Read only listed files; never discover or query an external source implicitly."""

    def __init__(self, storage_dir, *, parser=parse_document, max_file_bytes=50 * 1024 * 1024, model_settings=None):
        self.storage_dir = _io_path(Path(storage_dir))
        from .config import load_model_settings
        from .ocr_documents import (PROVIDER_CONTRACT_VERSION, RECOGNITION_CACHE_VERSION,
                                    parse_with_models, public_model_settings)
        self.model_settings = model_settings if model_settings is not None else load_model_settings()
        self._uses_model_parser = parser is parse_document
        self.parser = (lambda path, *, file_id, progress_callback=None, cache_dir=None: parse_with_models(
            path, file_id=file_id, settings=self.model_settings, progress_callback=progress_callback,
            cache_dir=cache_dir)) if self._uses_model_parser else parser
        self.max_file_bytes = max_file_bytes
        self.parser_revision = digest({
            "version": PARSER_VERSION,
            "implementation": sha256(package_files("compliance_service").joinpath("parsers.py").read_bytes()).hexdigest(),
            "dependencies": {name: version(name) for name in ("pypdf", "python-docx", "openpyxl")},
            "models": public_model_settings(self.model_settings),
            "recognition_implementation": RECOGNITION_CACHE_VERSION,
            "provider_implementation": PROVIDER_CONTRACT_VERSION,
        })

    def __call__(self, run: dict, *, progress_callback=None) -> dict:
        from .fact_extraction import extract_facts
        from .association import associate_facts
        from .rulebook import load_rulebook

        spec = run["spec"]
        baseline = load_rulebook(spec["rulebook_version"])
        if baseline["revision"] != spec["rulebook_revision"]:
            raise InvalidRequest("accepted rulebook revision is no longer available")
        manifest_path = _io_path(Path(spec["scope"]["manifest"]))
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        self._validate_manifest(manifest, spec)
        root = Path(manifest.get("root", str(manifest_path.parent))).expanduser()
        if not root.is_absolute():
            root = manifest_path.parent / root
        root = _io_path(root)
        owner = {"run_id": run["run_id"], **{
            key: spec[key] for key in ("source_system", "mine_code", "environment")
        }}
        namespace = digest({key: spec[key] for key in ("source_system", "mine_code", "environment")})
        started_at = datetime.now(timezone.utc).isoformat()
        clock_start = perf_counter()
        def progress(**event):
            if progress_callback is not None:
                progress_callback(event)
        entries = list(manifest["files"])
        handler = (spec.get("inspection_item") or {}).get("handler", spec["item_code"])
        staged = run.get("catalog_profile") == "oceanbase" and handler == "stop_work_unload"
        spool_parsed = self._uses_model_parser and any(self.model_settings.get(kind, {}).get("enabled") for kind in ("ocr", "vision"))
        if staged:
            entries.sort(key=lambda entry: (STOP_WORK_ORDER.get(entry.get("material_group"), 9),
                                            entry.get("report_date") or "", entry["file_id"]))
        progress(stage="source", event="source_started", file_total=len(entries), status="running")
        files, parsed_files, gaps, coverage = [], [], [], []
        advisories = []
        parsed_count = reused_count = invalidated_count = 0
        screening_done = False
        for file_index, entry in enumerate(entries, 1):
            if staged and not screening_done and STOP_WORK_ORDER.get(entry.get("material_group"), 9) > 1:
                # A partial scan is progress only. Final findings still require all
                # relevant documents and the normal evidence/association gates.
                provisional = []
                for artifact, parsed_ref in parsed_files:
                    parsed = json.loads(parsed_ref.read_text(encoding="utf-8")) if isinstance(parsed_ref, Path) else parsed_ref
                    for block in parsed["blocks"]:
                        provisional.append({**block, "evidence_id": digest({"file_id": artifact["file_id"],
                                            "block_id": block["block_id"]}), "file_id": artifact["file_id"],
                                            "source_role": artifact["role"],
                                            "material_group": artifact.get("material_group")})
                screened = extract_facts(provisional, allow_unbound=True)
                progress(stage="screening", event="finished", status="provisional",
                         file_total=len(files), evidence_count=len(provisional),
                         fact_count=len(screened["facts"]), candidate_count=len(screened["candidates"]))
                screening_done = True
            file_id = entry["file_id"]
            file_started = perf_counter()
            progress(stage="source", event="file_started", file_id=file_id,
                     file_index=file_index, file_total=len(entries),
                     file_name=entry.get("original_name", entry["path"]), status="running")
            artifact = {"file_id": file_id, "source_path": entry["path"], "role": entry["role"],
                        "content_hash": None, "status": "failed"}
            for key in ("original_name", "material_group", "report_date", "source_metadata"):
                if key in entry:
                    artifact[key] = entry[key]
            try:
                original = (root / entry["path"]).resolve()
                if not original.is_relative_to(root):
                    raise InvalidRequest("file path escapes declared source root")
                if original.stat().st_size > self.max_file_bytes:
                    raise InvalidRequest("file exceeds local acquisition limit")
                data = original.read_bytes()
                if len(data) > self.max_file_bytes:
                    raise InvalidRequest("file exceeds local acquisition limit")
                content_hash = sha256(data).hexdigest()
                saved = self.storage_dir / namespace / "objects" / (content_hash + original.suffix.lower())
                if saved.exists():
                    if sha256(saved.read_bytes()).hexdigest() != content_hash:
                        raise InvalidRequest("immutable original checksum mismatch")
                else:
                    _atomic_bytes(saved, data)
                cache_key = digest({"hash": content_hash, "suffix": original.suffix.lower(), "parser": self.parser_revision})
                cached_path = self.storage_dir / namespace / "parsed" / (cache_key + ".json")
                parsed = None
                if cached_path.exists():
                    try:
                        cached = json.loads(cached_path.read_text(encoding="utf-8"))
                        payload = cached["payload"]
                        if (cached.get("parser_revision") != self.parser_revision
                                or cached.get("payload_digest") != digest(payload)
                                or payload.get("content_hash") != content_hash
                                or _has_failed_recognition(payload)
                                or not all(isinstance(payload.get(name), list) for name in ("blocks", "coverage", "issues"))):
                            raise InvalidRequest("cached parse checksum or schema mismatch")
                        parsed = deepcopy(payload)
                        parsed["file_id"] = file_id
                        reused_count += 1
                        progress(stage="parse", event="cache_reused", file_id=file_id, status="completed")
                    except (StaleLease, RunPaused):
                        raise
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        invalidated_count += 1
                if parsed is None:
                    if self._uses_model_parser:
                        parsed = self.parser(saved, file_id=file_id, progress_callback=progress_callback,
                                             cache_dir=self.storage_dir / namespace / "recognition")
                    else:
                        parsed = self.parser(saved, file_id=file_id)
                    if parsed.get("content_hash") != content_hash:
                        raise InvalidRequest("parser did not read frozen source bytes")
                    if not _has_failed_recognition(parsed):
                        _atomic_bytes(cached_path, canonical({"parser_revision": self.parser_revision,
                                                             "payload_digest": digest(parsed), "payload": parsed}).encode("utf-8"))
                    parsed_count += 1
                artifact.update(content_hash=content_hash, byte_size=len(data), original_path=str(saved),
                                format=parsed["format"], status="acquired", parser_version=PARSER_VERSION,
                                parser_revision=self.parser_revision)
                if spool_parsed:
                    spool_path = self.storage_dir / namespace / "run-parsed" / run["run_id"] / (digest({"file_id": file_id, "content_hash": content_hash}) + ".json")
                    _atomic_bytes(spool_path, canonical(parsed).encode("utf-8"))
                    parsed_files.append((artifact, spool_path))
                else:
                    parsed_files.append((artifact, parsed))
                for unit in parsed["coverage"]:
                    coverage.append({"file_id": file_id, **unit})
                    if unit["state"] not in {"readable", "empty"}:
                        gaps.append(f"{file_id}/{unit['unit_id']}: {unit['state']} {unit.get('reason', '')}".strip())
                    for issue in (unit.get("recognition") or {}).get("issues", []):
                        gaps.append(f"{file_id}/{unit['unit_id']}: {issue}")
                if not parsed["coverage"]:
                    gaps.append(f"{file_id}: missing parser coverage")
                if spool_parsed:
                    del parsed
                    gc.collect()
            except (StaleLease, RunPaused):
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                artifact["error"] = type(exc).__name__ + ": " + str(exc)
                coverage.append({"file_id": file_id, "unit_id": "document", "state": "failed", "reason": type(exc).__name__})
                gaps.append(f"{file_id}: acquisition failed ({type(exc).__name__})")
            files.append(artifact)
            progress(stage="source", event="file_finished", file_id=file_id,
                     file_index=file_index, file_total=len(entries),
                     file_name=entry.get("original_name", entry["path"]), status=artifact["status"],
                     elapsed_ms=round((perf_counter()-file_started)*1000, 3))
        snapshot_id = digest({**owner, "manifest_hash": sha256(manifest_bytes).hexdigest(), "files": files})
        evidence = []
        for artifact, parsed_ref in parsed_files:
            parsed = json.loads(parsed_ref.read_text(encoding="utf-8")) if isinstance(parsed_ref, Path) else parsed_ref
            for block in parsed["blocks"]:
                evidence_id = digest({**owner, "snapshot_id": snapshot_id, "file_id": artifact["file_id"],
                                      "block_id": block["block_id"]})
                evidence.append({**owner, **block, "evidence_id": evidence_id, "snapshot_id": snapshot_id,
                                 "file_id": artifact["file_id"], "content_hash": artifact["content_hash"],
                                 "source_role": artifact["role"],
                                 "original_name": artifact.get("original_name", artifact.get("source_path")),
                                 "material_group": artifact.get("material_group")})
        progress(stage="extraction", event="extraction_started", status="running", evidence_count=len(evidence))
        extract_started = perf_counter()
        extraction = extract_facts(evidence, allow_unbound=True)
        start, end = manifest["period"]["start"], manifest["period"]["end"]
        facts = [fact for fact in extraction["facts"]
                 if fact["kind"] in {"threshold", "requirements", "risk_assessment", "design"}
                 or (fact.get("time") and start <= fact["time"][:10] <= end)]
        association = associate_facts(facts, observation={"start": start, "end": end})
        facts = association["facts"]
        from .daily_clues import extract_daily_clues
        from .daily_episodes import aggregate_daily_findings
        daily_files = [{**file, "mine_code": spec["mine_code"], "source_metadata": file.get("source_metadata") or {}}
                       for file in files]
        daily_relevant = any(file.get("material_group") in {"prevention_daily", "dispatch_daily", "analysis", "release"}
                             for file in files)
        daily_candidate_evidence = []
        if daily_relevant:
            for artifact, parsed_ref in parsed_files:
                parsed = json.loads(parsed_ref.read_text(encoding="utf-8")) if isinstance(parsed_ref, Path) else parsed_ref
                if artifact.get("material_group") not in {"prevention_daily", "dispatch_daily", "analysis", "release"}:
                    continue
                for unit in parsed.get("coverage", []):
                    for image in (unit.get("recognition") or {}).get("images", []):
                        for candidate in image.get("recognition_candidates", []):
                            text = str(candidate.get("text") or "").strip()
                            if not text:
                                continue
                            candidate_id = digest({"run_id": run["run_id"], "file_id": artifact["file_id"],
                                                   "unit_id": unit.get("unit_id"), "provider": candidate.get("provider")})
                            daily_candidate_evidence.append({
                                **owner, "snapshot_id": snapshot_id, "content_hash": artifact["content_hash"],
                                "candidate_id": candidate_id, "block_id": candidate_id,
                                "file_id": artifact["file_id"], "text": text,
                                "source_role": artifact["role"], "material_group": artifact.get("material_group"),
                                "kind": "model_candidate", "metadata": {"recognition_quality": "candidate"},
                                "locator": {"unit_id": unit.get("unit_id"), "provider": candidate.get("provider")},
                            })
            daily_extraction = extract_daily_clues(evidence + daily_candidate_evidence, files,
                                                   observation={"start": start, "end": end})
            daily_assessment = aggregate_daily_findings(daily_extraction["clues"], daily_extraction["arrangements"],
                                                        daily_files, observation={"start": start, "end": end})
        else:
            daily_extraction = {"clues": [], "arrangements": [], "gaps": []}
            daily_assessment = {"issues": [], "episodes": [], "gaps": [], "summary": {
                "clue_count": 0, "issue_count": 0, "episode_count": 0, "confirmed_count": 0,
                "candidate_count": 0, "unverifiable_count": 0, "version_conflict_count": 0}}
        advisories.extend(extraction.get("advisories", []))
        gaps.extend(extraction["gaps"])
        gaps.extend(association["gaps"])
        if daily_relevant:
            gaps.extend("daily_clue: " + str(gap) for gap in daily_extraction["gaps"])
            gaps.extend("daily_episode: " + str(gap) for gap in daily_assessment["gaps"])
        if extraction["candidates"]:
            gaps.append(f"{len(extraction['candidates'])} unresolved event/action candidates")
        if not files:
            gaps.append("no files in explicitly declared input scope")
        if manifest["source_complete"] is not True:
            gaps.append("source observation completeness not confirmed")
        progress(stage="extraction", event="extraction_finished", status="completed", fact_count=len(facts),
                 candidate_count=len(extraction["candidates"]), evidence_count=len(evidence),
                 elapsed_ms=round((perf_counter()-extract_started)*1000, 3))
        progress(stage="source", event="source_finished", status="completed", file_total=len(files),
                 elapsed_ms=round((perf_counter()-clock_start)*1000, 3))
        if spool_parsed:
            shutil.rmtree(self.storage_dir / namespace / "run-parsed" / run["run_id"], ignore_errors=True)
        return {
            **owner, "snapshot_id": snapshot_id, "profile": "local-documents",
            "parser_version": PARSER_VERSION, "parser_revision": self.parser_revision,
            "manifest_hash": sha256(manifest_bytes).hexdigest(),
            "observation": {"start": start, "end": end, "query_started_at": started_at,
                            "query_finished_at": datetime.now(timezone.utc).isoformat(),
                            "kind": "explicit-local-manifest", "scope": "listed-files-only"},
            "files": files, "evidence": evidence, "facts": facts, "candidates": extraction["candidates"],
            "association_links": association["links"], "rule_baseline": baseline,
            "daily_clues": daily_extraction["clues"], "daily_arrangements": daily_extraction["arrangements"],
            "daily_candidate_evidence": daily_candidate_evidence,
            "daily_gaps": daily_extraction["gaps"], "daily_assessment": daily_assessment,
            "coverage": coverage, "coverage_gaps": list(dict.fromkeys(gaps)),
            # Format advisories (unknown column names, etc.) are reported but do
            # not count as missing evidence, so they never block completeness.
            "advisories": list(dict.fromkeys(advisories)),
            "page_receipts": _page_receipts(parsed_files),
            "source_complete": not gaps and bool(files),
            "cache": {"parsed": parsed_count, "reused": reused_count, "invalidated": invalidated_count},
        }

    @staticmethod
    def _validate_manifest(manifest: dict, spec: dict):
        if not isinstance(manifest, dict):
            raise InvalidRequest("manifest must be an object")
        for name in ("source_system", "mine_code", "environment"):
            if manifest.get(name) != spec[name]:
                raise InvalidRequest(f"manifest ownership mismatch: {name}")
        period = manifest.get("period")
        if not isinstance(period, dict) or set(period) != {"start", "end"}:
            raise InvalidRequest("explicit period start/end required")
        for value in period.values():
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise InvalidRequest("period must use YYYY-MM-DD")
        if period["start"] > period["end"]:
            raise InvalidRequest("inverted period")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > 1000:
            raise InvalidRequest("manifest requires at most 1000 files")
        if type(manifest.get("source_complete")) is not bool:
            raise InvalidRequest("source_complete must explicitly be a boolean")
        seen = set()
        for entry in files:
            if not isinstance(entry, dict) or not set(entry) <= {"file_id", "path", "role", "original_name", "material_group", "report_date", "source_metadata"}:
                raise InvalidRequest("invalid file entry")
            if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("file_id", "path", "role")):
                raise InvalidRequest("file_id, path and role are required")
            if entry["role"] not in ROLES or entry["file_id"] in seen:
                raise InvalidRequest("invalid role or duplicate file_id")
            for metadata_key in ("original_name", "material_group", "report_date"):
                if metadata_key in entry and (not isinstance(entry[metadata_key], str) or not entry[metadata_key].strip()):
                    raise InvalidRequest(f"{metadata_key} must be a non-empty string")
            if "source_metadata" in entry:
                metadata = entry["source_metadata"]
                if (not isinstance(metadata, dict)
                        or any(not isinstance(key, str) or len(key) > 64 for key in metadata)
                        or any(not isinstance(value, (str, int, float, bool)) for value in metadata.values())):
                    raise InvalidRequest("source_metadata must contain scalar values")
            seen.add(entry["file_id"])
