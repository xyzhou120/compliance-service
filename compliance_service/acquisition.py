"""Attachment acquisition from an OceanBase query snapshot.

Downloads are keyed by attachment_id, streamed to .part, bounded, checked by
magic and SHA-256, and never follow redirects.  The resulting manifest is a
local handoff to LocalDocumentSource; source_complete stays false because a
catalog listing cannot prove that the declared material scope is complete.
"""
from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import shutil
import re
import socket
import ssl
from tempfile import NamedTemporaryFile
from time import perf_counter, sleep
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler

from .contracts import InvalidRequest, canonical, digest
from .callbacks import URLPolicy, CallbackPermanentError

MAX_BYTES = 50 * 1024 * 1024
MAGIC = {".pdf": b"%PDF-", ".docx": b"PK", ".xlsx": b"PK", ".xlsm": b"PK"}

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InvalidRequest("attachment redirects are not followed")

class _PinnedConnection(HTTPSConnection):
    def __init__(self, *args, certificate_sha256, **kwargs):
        super().__init__(*args, **kwargs)
        self.certificate_sha256 = certificate_sha256

    def connect(self):
        super().connect()
        actual = sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not compare_digest(actual, self.certificate_sha256):
            self.close()
            raise ssl.SSLCertVerificationError("attachment certificate does not match configured SHA256")

class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, pins):
        super().__init__()
        self.pins = {}
        if not isinstance(pins, dict):
            raise InvalidRequest("attachment TLS pins must map HTTPS origins to SHA256 fingerprints")
        for origin, fingerprint in pins.items():
            if not isinstance(origin, str) or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
                raise InvalidRequest("invalid attachment TLS certificate pin")
            try:
                parts = urlsplit(origin)
                valid = parts.scheme == "https" and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment) and parts.path in {"", "/"}
                key = (parts.hostname, parts.port or 443)
            except ValueError:
                valid = False
            if not valid:
                raise InvalidRequest("attachment TLS pin must identify an HTTPS origin")
            self.pins[key] = fingerprint.lower()

    def https_open(self, request):
        parts = urlsplit(request.full_url)
        fingerprint = self.pins.get((parts.hostname, parts.port or 443))
        if fingerprint is None:
            return super().https_open(request)
        # Check the exact configured certificate before sending the HTTP request.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        def connection(host, **kwargs):
            return _PinnedConnection(host, certificate_sha256=fingerprint, **kwargs)
        return self.do_open(connection, request, context=context)


class ReceiptWriteError(OSError):
    """A durable receipt could not be atomically published after retries."""


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 4, delay_seconds: float = 0.08):
    """Publish a small receipt despite short Windows file sharing locks."""
    last = None
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(delay_seconds * (2 ** attempt))
    raise ReceiptWriteError(f"atomic receipt replace failed after {attempts} attempts: {last}") from last

def _safe_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    if name in {"", ".", ".."} or any(ch in name for ch in "\\/:*?\"<>|"):
        return fallback
    return name[:180]

def _magic_ok(path: Path, suffix: str | None = None) -> bool:
    magic = MAGIC.get((suffix or path.suffix).lower())
    if magic is None:
        return False
    with path.open("rb") as stream:
        return stream.read(len(magic)).startswith(magic)

def _stream_copy(source, target: Path, *, max_bytes: int = MAX_BYTES) -> tuple[int, str]:
    digestor = sha256(); count = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk: break
            count += len(chunk)
            if count > max_bytes: raise InvalidRequest("attachment exceeds maximum size")
            digestor.update(chunk); out.write(chunk)
    return count, digestor.hexdigest()

class AttachmentAcquisition:
    def __init__(self, *, opener=None, fetcher: Callable[[str], Any] | None = None, allowed_hosts: set[str] | None = None,
                 url_policy: URLPolicy | None = None, max_bytes: int = MAX_BYTES,
                 source_system: str = "oceanbase", environment: str = "test", tls_certificate_sha256: dict | None = None):
        handler = _PinnedHTTPSHandler(tls_certificate_sha256) if tls_certificate_sha256 is not None else HTTPSHandler()
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect(), handler)
        self.fetcher = fetcher
        self.allowed_hosts = {str(host).lower().rstrip(".") for host in (allowed_hosts or set())}
        if url_policy is None and not self.allowed_hosts:
            raise ValueError("attachment URL allowlist is required")
        self.url_policy = url_policy or URLPolicy(allowed_hosts=frozenset(self.allowed_hosts))
        self.max_bytes = max_bytes
        self.source_system = source_system
        self.environment = environment

    def _download(self, url: str, path: Path) -> tuple[int, str]:
        try:
            url = self.url_policy.validate(url)
        except CallbackPermanentError as exc:
            raise InvalidRequest(str(exc)) from exc
        parts = urlsplit(url)
        if self.fetcher is not None:
            stream = self.fetcher(url)
            try:
                return _stream_copy(stream, path, max_bytes=self.max_bytes)
            finally:
                stream.close()
        try:
            addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
            if not addresses:
                raise InvalidRequest("attachment host has no resolved address")
            for address in addresses:
                self.url_policy.check_address(address[4][0])
        except socket.gaierror as exc:
            raise InvalidRequest("attachment DNS lookup failed") from exc
        request = Request(url, method="GET")
        with self.opener.open(request, timeout=40) as response:
            if getattr(response, "status", 200) != 200:
                raise InvalidRequest(f"attachment HTTP {getattr(response, 'status', 0)}")
            length = response.headers.get("Content-Length")
            if length and int(length) > self.max_bytes:
                raise InvalidRequest("attachment exceeds maximum size")
            return _stream_copy(response, path, max_bytes=self.max_bytes)

    def prepare(self, snapshot: dict[str, Any], target_dir: str | Path, *, progress_callback: Callable[..., Any] | None = None) -> str:
        if not isinstance(snapshot, dict) or snapshot.get("profile") != "oceanbase-catalog":
            raise InvalidRequest("an OceanBase catalog snapshot is required")
        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            raise InvalidRequest("snapshot rows are required")
        root = Path(target_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        snapshot_identity = digest(snapshot)
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            prior_identity = (previous.get("acquisition") or {}).get("snapshot_identity")
            if prior_identity is not None and prior_identity != snapshot_identity:
                raise InvalidRequest("acquisition target already belongs to another input snapshot")
        receipt_path = root / "acquisition-receipts.json"
        receipts = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {"snapshot_identity": snapshot_identity, "files": {}}
        if receipts.get("snapshot_identity") != snapshot_identity:
            raise InvalidRequest("acquisition target already belongs to another input snapshot")
        def persist_receipts():
            partial = None
            try:
                with NamedTemporaryFile(dir=root, prefix="acquisition-receipts-", suffix=".part", delete=False) as stream:
                    partial = Path(stream.name)
                    stream.write(canonical(receipts).encode("utf-8"))
                _replace_with_retry(partial, receipt_path)
            except OSError as exc:
                raise ReceiptWriteError(f"attachment receipt persistence failed: {exc}") from exc
            finally:
                if partial is not None:
                    partial.unlink(missing_ok=True)

        receipt_failures = []
        try:
            persist_receipts()
        except ReceiptWriteError as exc:
            # The immutable snapshot and downloaded bytes may still be useful;
            # retain the failure separately and retry on the next run.
            receipt_failures.append({"stage": "receipt", "operation": "bootstrap", "error": str(exc)})
        files, failures, seen = [], [], set()
        prior_entries = {}
        if manifest_path.exists():
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (prior.get("acquisition") or {}).get("snapshot_identity") == snapshot_identity:
                    prior_entries = {str(item.get("file_id")): item for item in prior.get("files", [])
                                     if isinstance(item, dict) and item.get("file_id")}
            except (OSError, ValueError, TypeError):
                prior_entries = {}
        def progress(**event):
            if progress_callback is not None:
                progress_callback(event)
        progress(stage="acquisition", event="started", row_total=len(rows), status="running")
        for row in rows:
            aid = str(row.get("attachment_id") or "").strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            file_started = perf_counter()
            progress(stage="acquisition", event="attachment_started", file_id=aid, status="running")
            url = row.get("attachment_url")
            name = _safe_name(str(row.get("attachment_name") or ""), aid)
            suffix = Path(name).suffix.lower()
            safe_id = digest({"attachment_id": aid})[:40]
            path = root / f"{safe_id}{suffix}"
            partial = Path(str(path) + ".part")
            error = None
            failure_stage = "download"
            try:
                if suffix not in MAGIC:
                    failure_stage = "validation"
                    raise InvalidRequest("unsupported attachment format")
                receipt = receipts["files"].get(aid)
                prior_entry = prior_entries.get(aid)
                if receipt is None and prior_entry:
                    prior_metadata = prior_entry.get("source_metadata") or {}
                    prior_path = root / str(prior_entry.get("path") or "")
                    if (prior_metadata.get("attachment_url") == url and prior_metadata.get("content_hash")
                            and prior_path == path and path.is_file() and _magic_ok(path, suffix)):
                        recovered_hash = sha256(path.read_bytes()).hexdigest()
                        if recovered_hash == prior_metadata["content_hash"]:
                            receipts["files"][aid] = {"url": url, "content_hash": recovered_hash,
                                                       "byte_size": path.stat().st_size}
                            receipt = receipts["files"][aid]
                            try:
                                persist_receipts()
                            except ReceiptWriteError as exc:
                                receipt_failures.append({"attachment_id": aid, "stage": "receipt",
                                                         "operation": "recover", "error": str(exc)})
                if receipt:
                    failure_stage = "validation"
                    if receipt.get("url") != url or not path.exists() or not _magic_ok(path, suffix):
                        raise InvalidRequest("frozen attachment receipt does not match source")
                    size = path.stat().st_size
                    content_hash = sha256(path.read_bytes()).hexdigest()
                    if size > self.max_bytes or receipt.get("content_hash") != content_hash or receipt.get("byte_size") != size:
                        raise InvalidRequest("frozen attachment bytes differ from receipt")
                else:
                    if not isinstance(url, str) or not url.strip():
                        failure_stage = "validation"
                        raise InvalidRequest("attachment URL is missing")
                    failure_stage = "download"
                    size, content_hash = self._download(url, partial)
                    failure_stage = "validation"
                    if not _magic_ok(partial, suffix):
                        raise InvalidRequest("attachment magic does not match extension")
                    expected_hash = row.get("content_hash")
                    if expected_hash and expected_hash != content_hash:
                        raise InvalidRequest("attachment checksum differs from frozen row")
                    partial.replace(path)
                    receipts["files"][aid] = {"url": url, "content_hash": content_hash, "byte_size": size}
                    try:
                        persist_receipts()
                    except ReceiptWriteError as exc:
                        receipt_failures.append({"attachment_id": aid, "stage": "receipt",
                                                 "operation": "publish", "error": str(exc)})
                metadata = {"attachment_id": aid, "snapshot_id": str(snapshot.get("snapshot_id") or ""),
                            "byte_size": size, "content_hash": content_hash}
                for key in ("file_name_id", "attachment_url", "source_row"):
                    value = row.get(key)
                    if value is not None:
                        metadata[key] = str(value) if key != "source_row" else value
                entry = {"file_id": aid, "path": path.name, "role": row.get("role", "actual_record"),
                         "original_name": name, "source_metadata": metadata}
                for key in ("material_group", "report_date"):
                    if row.get(key) is not None:
                        entry[key] = str(row[key])
                files.append(entry)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                error = type(exc).__name__ + ": " + str(exc)
                failures.append({"attachment_id": aid, "status": "failed", "stage": failure_stage, "error": error})
            # A pause/fence exception must escape rather than become a failed attachment.
            progress(stage="acquisition", event="attachment_finished", file_id=aid,
                     status="failed" if error else "completed", elapsed_ms=round((perf_counter() - file_started) * 1000, 3))
        manifest = {"source_system": snapshot.get("source_system", self.source_system), "mine_code": snapshot.get("mine_code"),
                    "environment": snapshot.get("environment", self.environment), "period": snapshot.get("period") or {},
                    "source_complete": False, "root": str(root), "files": files,
                    "acquisition": {"snapshot_id": snapshot.get("snapshot_id"),
                                    "query_version": (snapshot.get("query") or {}).get("query_version"),
                                    "failures": failures, "catalog_index_only": True}}
        if receipt_failures:
            manifest["acquisition"]["receipt_failures"] = receipt_failures
            manifest["acquisition"]["snapshot_identity"] = snapshot_identity
        output = root / "manifest.json"
        text = canonical(manifest)
        if output.exists() and output.read_text(encoding="utf-8") != text:
            previous = json.loads(output.read_text(encoding="utf-8"))
            previous_acquisition = previous.get("acquisition") or {}
            if not (previous_acquisition.get("failures") or previous_acquisition.get("receipt_failures")):
                raise InvalidRequest("completed acquisition manifest is immutable")
        with NamedTemporaryFile(dir=root, prefix="manifest-", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text.encode("utf-8"))
        try:
            _replace_with_retry(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        progress(stage="acquisition", event="finished", status="completed", file_count=len(files), failure_count=len(failures))
        return str(output.resolve())

__all__ = ["AttachmentAcquisition"]
