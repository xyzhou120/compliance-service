"""Explicit local-debug attachment acquisition and automatically generated manifests."""

from base64 import b64decode
from binascii import Error as Base64Error
import json
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
from uuid import uuid4

from fastapi import HTTPException

from .contracts import InvalidRequest, digest, parse_batch
from .sources import LocalDocumentSource


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
# Base64 increases file size by one third; leave one MiB for metadata.
MAX_REQUEST_BYTES = (MAX_BUNDLE_BYTES + 2) // 3 * 4 + 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_BOTH = ["stop_work_unload", "relief_effects"]
MATERIAL_GROUPS = [
    {"group_id": group_id, "title": title, "role": role, "items": list(items), "description": description}
    for group_id, title, role, items, description in (
        ("risk_assessment", "冲击危险性评价", "requirement", _BOTH, "适用工作面、危险等级和评价范围"),
        ("design", "防冲设计与措施", "requirement", _BOTH, "卸压方案、设计参数和效果检验标准"),
        ("monitoring", "监测预警记录", "actual_record", _BOTH, "预警、超限及冲击危险触发记录，可提供多份"),
        ("prevention_daily", "防冲工作日报", "actual_record", _BOTH, "逐日处置、停工撤人与卸压记录，可提供多份日报"),
        ("dispatch_daily", "调度日报与调度记录", "actual_record", _BOTH, "停工、断电、撤人、上报和调度处置，可提供多份"),
        ("analysis", "分析研判与告知", "actual_record", _BOTH, "研判结论、签字和告知记录"),
        ("relief_acceptance", "卸压施工与验收", "actual_record", _BOTH, "钻孔、爆破等施工参数、区域和验收资料"),
        ("effect_test", "解危效果检验", "actual_record", _BOTH, "效果检验原始数据、测点、测值和检验结论"),
        ("release", "技术确认与调度解除", "actual_record", _BOTH, "技术负责人签署、解除通知和解除时序"),
        ("production", "恢复生产记录", "actual_record", _BOTH, "实际复工、生产时间和工作面记录"),
    )
]
_GROUPS = {group["group_id"]: group for group in MATERIAL_GROUPS}


async def read_bundle_body(request):
    """Bound the stream before JSON/base64 allocation, including chunked requests."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as exc:
            raise InvalidRequest("invalid content-length") from exc
        if size < 0:
            raise InvalidRequest("invalid content-length")
        if size > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="debug bundle request exceeds size limit")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="debug bundle request exceeds size limit")
        data.extend(chunk)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRequest("debug bundle requires valid JSON") from exc


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _attachment_name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise InvalidRequest("attachment name is required and must be at most 255 characters")
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name in {".", ".."}:
        raise InvalidRequest("attachment name must be a simple filename")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise InvalidRequest("unsupported attachment format; use PDF, DOCX, XLSX or an image")
    return name


def _url_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise InvalidRequest("attachment URL is required")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        _ = parts.port
    except ValueError as exc:
        raise InvalidRequest("attachment URL must use HTTP/HTTPS without embedded credentials") from exc
    if any(ord(char) < 32 for char in value):
        raise InvalidRequest("attachment URL contains invalid characters")
    return unquote(parts.path.rsplit("/", 1)[-1])


def _local_path(value, storage, namespace):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InvalidRequest("local_path requires an absolute file path")
    # An explicitly supplied file is approved; directory scanning is never used.
    path = Path(value).resolve()
    if path.name.startswith(".") or any(part.casefold() in {"config", ".config", ".git", ".codex", ".ssh", ".aws"} for part in path.parts):
        raise InvalidRequest("local attachment cannot read hidden or configuration directories")
    _attachment_name(path.name)
    for protected in (storage / "uploads", storage / "artifacts"):
        protected = protected.resolve()
        if path.is_relative_to(protected) and not path.is_relative_to(protected / namespace):
            raise InvalidRequest("local attachment belongs to another owner")
    if not path.is_file():
        raise InvalidRequest("local attachment file is unavailable")
    return path


def _copy_bounded(source, target, remaining):
    size = 0
    with target.open("xb") as output:
        while True:
            chunk = source.read(min(1024 * 1024, min(MAX_FILE_BYTES, remaining) - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES or size > remaining:
                raise InvalidRequest("attachment exceeds 50 MiB or bundle exceeds 200 MiB")
            output.write(chunk)
    if not size:
        raise InvalidRequest("attachment is empty")
    return size


def _download(url, target, remaining):
    try:
        parts = urlsplit(url)
        encoded_url = urlunsplit((parts.scheme, parts.netloc,
                                 quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                                 quote(parts.query, safe="%=&/:?@!$'()*+,;+-._~"), ""))
        with build_opener(_RejectRedirects()).open(Request(encoded_url, headers={"Accept": "*/*"}), timeout=30) as response:
            if response.status != 200:
                raise InvalidRequest("attachment URL did not return HTTP 200")
            return _copy_bounded(response, target, remaining)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise InvalidRequest("attachment URL redirects are not allowed; provide the final URL") from None
        raise InvalidRequest(f"attachment download failed (HTTP {exc.code})") from None
    except (URLError, OSError, ValueError):
        # Never include a signed URL, query token, or provider exception in the response.
        raise InvalidRequest("attachment download failed; verify the address and service connectivity") from None


def submit_bundle(service, body):
    if not isinstance(body, dict):
        raise InvalidRequest("debug bundle request must be an object")
    allowed = {"sourceSystem", "mineCode", "environment", "checkCycle", "ruleVersion", "items", "period", "source_complete", "attachments"}
    if set(body) - allowed:
        raise InvalidRequest("unknown debug bundle fields")
    attachments = body.get("attachments")
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= 1000:
        raise InvalidRequest("attachments must contain between 1 and 1000 entries")
    storage = Path(service.ledger.path).resolve().parent
    bundle_id = uuid4().hex
    owner = {"source_system": body.get("sourceSystem"), "mine_code": body.get("mineCode"), "environment": body.get("environment", "local")}
    namespace = digest(owner)
    directory = storage / "uploads" / namespace / bundle_id
    manifest_path = directory / "manifest.json"
    request = {key: body[key] for key in ("sourceSystem", "mineCode", "environment", "checkCycle", "items", "ruleVersion") if key in body}
    request.update(scope={"manifest": str(manifest_path)}, callbackMode="none")
    specs = parse_batch(request, inspection_config=service.inspection_registry.read()["config"])
    if any(set(item) != {"itemCode"} for item in body["items"]):
        raise InvalidRequest("debug bundle items may only specify itemCode")
    manifest = {**owner, "period": body.get("period"), "source_complete": body.get("source_complete"), "files": []}
    plans = []
    total_bytes = 0
    for index, item in enumerate(attachments):
        if not isinstance(item, dict) or item.get("group_id") not in _GROUPS:
            raise InvalidRequest("unknown attachment material group")
        kind = item.get("kind")
        fields = {"file": {"name", "data"}, "local_path": {"path"}, "url": {"url"}}.get(kind)
        if fields is None or set(item) - ({"group_id", "kind", "name", "source_metadata"} | fields):
            raise InvalidRequest("invalid attachment kind or fields")
        source_metadata = item.get("source_metadata")
        if source_metadata is not None:
            if not isinstance(source_metadata, dict) or any(not isinstance(k, str) or len(k) > 64 for k in source_metadata):
                raise InvalidRequest("source_metadata must be a small object")
            if any(not isinstance(v, (str, int, float, bool)) for v in source_metadata.values()):
                raise InvalidRequest("source_metadata values must be scalar")
        content = None
        if kind == "file":
            name = _attachment_name(item.get("name"))
            encoded = item.get("data")
            if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES + 2) // 3 * 4:
                raise InvalidRequest("file requires base64 data within 50 MiB")
            try:
                content = b64decode(encoded, validate=True)
            except (Base64Error, ValueError) as exc:
                raise InvalidRequest("file data must be valid base64") from exc
            if not content or len(content) > MAX_FILE_BYTES:
                raise InvalidRequest("attachment is empty or exceeds 50 MiB")
            total_bytes += len(content)
        elif kind == "local_path":
            content = _local_path(item.get("path"), storage, namespace)
            name = _attachment_name(item.get("name", content.name))
            if not 0 < content.stat().st_size <= MAX_FILE_BYTES:
                raise InvalidRequest("local attachment is empty or exceeds 50 MiB")
            total_bytes += content.stat().st_size
        else:
            inferred = _url_name(item.get("url"))
            name = _attachment_name(item.get("name", inferred))
        if total_bytes > MAX_BUNDLE_BYTES:
            raise InvalidRequest("bundle exceeds 200 MiB")
        file_id = f"{item['group_id']}-{index + 1:04d}"
        filename = file_id + Path(name).suffix.lower()
        entry = {"file_id": file_id, "path": filename, "role": _GROUPS[item["group_id"]]["role"],
                 "original_name": name, "material_group": item["group_id"]}
        report_date = (source_metadata or {}).get("report_date")
        if isinstance(report_date, str) and report_date.strip():
            entry["report_date"] = report_date.strip()
        if source_metadata:
            entry["source_metadata"] = dict(source_metadata)
        manifest["files"].append(entry)
        plans.append((item, name, content, directory / filename))
    try:
        LocalDocumentSource._validate_manifest(manifest, specs[0].to_dict())
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(str(exc)) from exc
    directory.mkdir(parents=True, exist_ok=False)
    try:
        actual_bytes = 0
        for item, name, content, target in plans:
            if item["kind"] == "file":
                actual_bytes += len(content)
                if actual_bytes > MAX_BUNDLE_BYTES:
                    raise InvalidRequest("bundle exceeds 200 MiB")
                target.write_bytes(content)
            elif item["kind"] == "local_path":
                with content.open("rb") as source:
                    actual_bytes += _copy_bounded(source, target, MAX_BUNDLE_BYTES - actual_bytes)
            else:
                actual_bytes += _download(item["url"], target, MAX_BUNDLE_BYTES - actual_bytes)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt = [{"file_id": entry["file_id"], "group_id": plan[0]["group_id"], "name": plan[1]}
                   for entry, plan in zip(manifest["files"], plans)]
    except Exception as exc:
        shutil.rmtree(directory)
        if isinstance(exc, OSError):
            raise InvalidRequest("could not store debug attachments") from None
        raise
    # The normal batch contract permits partial acceptance. Once submission starts,
    # keep the immutable input even if a later item cannot be accepted.
    accepted = service.submit(request)
    return {**accepted, "manifest_path": str(manifest_path), "bundle_id": bundle_id, "owner": owner, "attachments": receipt}
