"""Safe external callback mapping and transport.

The callback layer deliberately knows nothing about the inspection evaluator.  It
accepts an immutable result payload and turns it into the small wire contract
used by the upstream service.  Network access is opt-in through an explicit
URL allowlist; importing this module never opens a socket.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPResponse
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, OpenerDirector, ProxyHandler


class CallbackError(RuntimeError):
    """Base class for a callback delivery failure."""


class CallbackRetryableError(CallbackError):
    """A delivery can be retried without changing the saved payload."""


class CallbackPermanentError(CallbackError):
    """The URL, payload, authentication, or business response is invalid."""


@dataclass(frozen=True)
class URLPolicy:
    """Allowlist policy for callback and attachment URLs.

    ``allowed_hosts`` and/or ``allowed_origins`` are required.  Private and
    loopback addresses remain denied unless explicitly enabled for a controlled
    test or a deployment that has reviewed its network boundary.
    """

    allowed_hosts: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()
    allow_http: bool = False
    allow_private: bool = False
    allow_loopback: bool = False

    def __post_init__(self):
        object.__setattr__(self, "allowed_hosts", frozenset(str(v).lower().rstrip(".") for v in self.allowed_hosts))
        object.__setattr__(self, "allowed_origins", frozenset(str(v).lower().rstrip("/") for v in self.allowed_origins))
        if not self.allowed_hosts and not self.allowed_origins:
            raise ValueError("callback URL allowlist is required")

    def validate(self, url: str) -> str:
        if not isinstance(url, str) or not url or len(url) > 2048:
            raise CallbackPermanentError("callback URL must be a non-empty short string")
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise CallbackPermanentError("invalid callback URL") from exc
        if parsed.scheme.lower() not in ({"https", "http"} if self.allow_http else {"https"}):
            raise CallbackPermanentError("callback URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise CallbackPermanentError("callback URL must not contain credentials")
        if parsed.fragment or not parsed.hostname:
            raise CallbackPermanentError("callback URL must contain a host and no fragment")
        host = parsed.hostname.lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise CallbackPermanentError("invalid callback URL port") from exc
        origin = f"{parsed.scheme.lower()}://{host}{":" + str(port) if port else ""}"
        if host not in self.allowed_hosts and origin not in self.allowed_origins:
            raise CallbackPermanentError("callback URL host is not allowlisted")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            self.check_address(ip)
        return url

    def check_address(self, address) -> None:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback and not self.allow_loopback:
            raise CallbackPermanentError("loopback callback URL is denied")
        # Metadata/link-local and unspecified destinations are never legitimate
        # private deployment targets.  Loopback needs its separate test switch.
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise CallbackPermanentError("special callback URL address is denied")
        if ip.is_private and not (self.allow_private or (ip.is_loopback and self.allow_loopback)):
            raise CallbackPermanentError("private callback URL is denied")


def _iso_time(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("checkTime must include a timezone")
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("checkTime is required")
    # Validate common ISO forms while preserving the caller's wire value.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("checkTime must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("checkTime must include a timezone")
    return value


def map_callback(run: dict, result: dict, snapshot: dict | None, finalized_at: str | datetime) -> dict:
    """Map internal result/snapshot fields to one upstream result object."""
    spec = run.get("spec") or {}
    status = result.get("status")
    if status not in {"pass", "no_pass", "pending", "unverifiable", "failed"}:
        raise ValueError("invalid callback result")
    description = result.get("description")
    detail = result.get("detail")
    if not isinstance(description, str) or not description.strip() or not isinstance(detail, str) or not detail.strip():
        raise ValueError("description and detail are required")
    files = {f.get("file_id"): f for f in (snapshot or {}).get("files", []) if isinstance(f, dict)}
    evidence_by_file = {}
    for evidence in (snapshot or {}).get("evidence", []):
        if isinstance(evidence, dict):
            evidence_by_file.setdefault(evidence.get("file_id"), set()).add(evidence.get("evidence_id"))
    finding_refs = set()
    for finding in result.get("findings", []) or []:
        if isinstance(finding, dict) and finding.get("certainty") not in {"candidate", "unverifiable"}:
            finding_refs.update(finding.get("evidence_refs", []) or [])
    out_attachments: list[dict] = []
    for item in result.get("attachments", []) or [{"file_id": f} for f in files]:
        if not isinstance(item, dict):
            continue
        file_id = item.get("file_id", item.get("fileId"))
        source = files.get(file_id, {})
        metadata = source.get("source_metadata") if isinstance(source.get("source_metadata"), dict) else {}
        url = item.get("attachmentUrl") or item.get("attachment_url") or metadata.get("attachmentUrl") or metadata.get("attachment_url") or metadata.get("original_url") or metadata.get("url") or source.get("attachment_url")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            continue
        try:
            parsed_attachment = urlsplit(url)
            if not parsed_attachment.hostname or parsed_attachment.username is not None or parsed_attachment.password is not None:
                continue
        except ValueError:
            continue
        name = item.get("attachmentName") or item.get("attachment_name") or source.get("original_name") or "附件"
        name = str(name).replace("\\", "/").rsplit("/", 1)[-1] or "附件"
        cycle = item.get("attachmentCycle") or item.get("attachment_cycle") or metadata.get("attachment_cycle") or metadata.get("attachmentCycle") or source.get("report_date") or spec.get("check_cycle")
        attachment = {"attachmentCycle": cycle, "attachmentName": name, "attachmentUrl": url}
        content = item.get("attachmentContent")
        concrete_finding = bool(finding_refs & evidence_by_file.get(file_id, set()))
        if not content and concrete_finding:
            messages = []
            for finding in result.get("findings", []) or []:
                if (not isinstance(finding, dict) or finding.get("certainty") in {"candidate", "unverifiable"}
                        or not (set(finding.get("evidence_refs", []) or []) & evidence_by_file.get(file_id, set()))):
                    continue
                message = finding.get("description") or finding.get("detail") or finding.get("code")
                if message:
                    messages.append(str(message))
            content = "；".join(dict.fromkeys(messages))
        if isinstance(content, str) and content.strip() and concrete_finding:
            attachment["attachmentContent"] = content.strip()
        out_attachments.append(attachment)
    detail_parts = [detail.strip()]
    for issue in result.get("daily_issues", []) or []:
        if isinstance(issue, dict) and issue.get("description"):
            detail_parts.append(f"日报线索[{issue.get('certainty', 'unknown')}] {issue['description']}")
    for finding in result.get("findings", []) or []:
        if not isinstance(finding, dict):
            continue
        message = finding.get("description") or finding.get("detail") or finding.get("reason") or finding.get("code")
        if message:
            detail_parts.append(f"问题依据: {message}")
        refs = set(finding.get("evidence_refs", []) or [])
        for evidence in (snapshot or {}).get("evidence", []):
            if evidence.get("evidence_id") in refs:
                source = files.get(evidence.get("file_id"), {})
                basename = str(source.get("original_name") or "附件").replace("\\", "/").rsplit("/", 1)[-1]
                detail_parts.append(f"证据: {basename} {json.dumps(evidence.get('locator', {}), ensure_ascii=False, sort_keys=True)}")
    for gap in result.get("coverage_gaps", []) or []:
        detail_parts.append(f"资料缺口: {gap}")
    mine_id = str(spec.get("mine_id", spec.get("mineId", spec.get("mine_code", result.get("mine_code", "")))))
    cycle = spec.get("check_cycle", result.get("checkCycle"))
    context = f"矿井{mine_id} 检查周期{cycle}："
    summary = description.strip()
    if not summary.startswith(context):
        summary = context + summary
    return {
        "mineId": mine_id,
        "itemCode": str(spec.get("item_code", result.get("itemCode", ""))),
        "checkCycle": cycle,
        "checkTime": _iso_time(finalized_at),
        "checkResult": status,
        "description": summary,
        "detail": "\n".join(dict.fromkeys(detail_parts)),
        "attachments": out_attachments,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CallbackPermanentError("redirects are not allowed for callbacks")


class HttpCallbackClient:
    """Small stdlib-only client with bounded response and explicit URL policy."""

    def __init__(self, policy: URLPolicy, *, timeout: float = 10.0, max_response_bytes: int = 64 * 1024,
                 opener: OpenerDirector | None = None, resolver: Callable = socket.getaddrinfo,
                 extra_headers: dict[str, str] | None = None):
        if not 0 < timeout < 300:
            raise ValueError("timeout must be positive and bounded")
        if not 1 <= max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError("max_response_bytes is out of range")
        self.policy = policy
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        # Do not inherit ambient proxy settings: proxy DNS can bypass the
        # destination policy and make private-address checks meaningless.
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.resolver = resolver
        forbidden = {"content-type", "idempotency-key", "host", "content-length"}
        self.extra_headers = {}
        for key, value in (extra_headers or {}).items():
            if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                raise ValueError("invalid callback header name")
            if key.lower() in forbidden:
                raise ValueError(f"callback header is reserved: {key}")
            if not isinstance(value, str) or any(char in value for char in "\r\n"):
                raise ValueError("invalid callback header value")
            self.extra_headers[key] = value

    def send(self, url: str, payload: dict, delivery_id: str) -> dict:
        url = self.policy.validate(url)
        parsed_url = urlsplit(url)
        try:
            addresses = self.resolver(parsed_url.hostname, parsed_url.port or (443 if parsed_url.scheme == "https" else 80), type=socket.SOCK_STREAM)
            if not addresses:
                raise CallbackRetryableError("callback host has no resolved address")
            for address in addresses:
                self.policy.check_address(address[4][0])
        except socket.gaierror as exc:
            raise CallbackRetryableError("callback DNS lookup failed") from exc
        if not isinstance(delivery_id, str) or not delivery_id.strip():
            raise ValueError("delivery_id is required")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = dict(self.extra_headers)
        headers.update({
            "Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": delivery_id,
        })
        request = Request(url, data=body, method="POST", headers=headers)
        try:
            response: HTTPResponse = self.opener.open(request, timeout=self.timeout)
            try:
                status = int(response.getcode())
                length = response.headers.get("Content-Length")
                try:
                    declared = int(length) if length else None
                except ValueError as exc:
                    raise CallbackPermanentError("callback response has invalid Content-Length") from exc
                if declared is not None and declared > self.max_response_bytes:
                    raise CallbackPermanentError("callback response is too large")
                data = response.read(self.max_response_bytes + 1)
            finally:
                response.close()
        except HTTPError as exc:
            if 500 <= exc.code <= 599:
                raise CallbackRetryableError(f"callback HTTP {exc.code}") from exc
            raise CallbackPermanentError(f"callback HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise CallbackRetryableError("callback timed out") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise CallbackRetryableError("callback timed out") from exc
            raise CallbackRetryableError(f"callback network error: {reason}") from exc
        except CallbackError:
            raise
        except OSError as exc:
            raise CallbackRetryableError(f"callback network error: {exc}") from exc
        if len(data) > self.max_response_bytes:
            raise CallbackPermanentError("callback response is too large")
        if not 200 <= status < 300:
            raise CallbackRetryableError(f"callback HTTP {status}") if status >= 500 else CallbackPermanentError(f"callback HTTP {status}")
        try:
            decoded = json.loads(data.decode("utf-8")) if data.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CallbackPermanentError("callback response is not JSON") from exc
        if not isinstance(decoded, dict):
            raise CallbackPermanentError("callback response must be a JSON object")
        if decoded.get("code") != 200 or decoded.get("success") is not True:
            raise CallbackPermanentError("callback business response was not successful")
        return {"http_status": status, "response": decoded}
