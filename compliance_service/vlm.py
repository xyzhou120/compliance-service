"""Vision channel used when the layout OCR cannot give a table grid.

The shared vision model already produced correct markdown/HTML tables on real
pages where PaddleOCR-VL collapsed the table to one cell. This module wraps that
call with the same bounds as the OCR channels and refuses anything the gates
cannot verify.
"""

from __future__ import annotations

import base64
import json
import os
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib import request as urlrequest

from .canonical import Table, Locator, tables_from_html

DEFAULT_MODEL = "qwen36-27b"
DEFAULT_BASE = "http://172.26.1.84:4000/v1"
TABLE_PROMPT = (
    "只输出 HTML <table>...</table>，不要解释。要求：\n"
    "1) 逐字保留原文文字与数字，指数写成 10^4 这种形式；\n"
    "2) 合并单元格用 colspan/rowspan 表达；\n"
    "3) 看不清的单元格留空，不要猜、不要补写；\n"
    "4) 不要输出表格以外的任何文字。")


class VisionUnavailable(RuntimeError):
    """The vision service failed; the caller must retry later."""


class VisionDeadlineExceeded(VisionUnavailable):
    """The wall-clock deadline passed; the connection was closed on purpose."""


def build_payload(model: str, image: bytes, prompt: str, *, max_tokens: int,
                  enable_thinking: bool = False, stream: bool = True) -> dict:
    """Small, bounded request: table-only HTML, no hidden thinking, capped output."""
    payload = {"model": model, "temperature": 0,
               "max_tokens": int(max_tokens),
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {
                       "url": "data:image/png;base64," + base64.b64encode(bytes(image)).decode()}}]}],
               "enable_thinking": bool(enable_thinking),
               "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}
    if stream:
        payload["stream"] = True
    return payload


def parse_sse(lines) -> str:
    """Join OpenAI-style SSE deltas into one text answer."""
    parts = []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        try:
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError):
            continue
        content = delta.get("content")
        if content:
            parts.append(content)
    return "".join(parts)


def extract_html(response_text: str) -> str:
    text = str(response_text or "")
    start = text.lower().find("<table")
    end = text.lower().rfind("</table>")
    if start < 0 or end < 0:
        return ""
    return text[start:end + len("</table>")]


def validate_table(table: Table, *, min_filled: float = 0.25) -> dict:
    """Gate a transcribed table: header, data rows and cell fill ratio.

    Real mine ledgers leave many cells empty, so the fill ratio only rejects
    obviously fractured output; structural checks stay strict.
    """
    reasons = []
    paths = table.column_paths()
    if table.column_count < 2:
        reasons.append("NO_COLUMNS")
    if not paths:
        reasons.append("NO_HEADER")
    data_rows = table.data_rows()
    if not data_rows:
        reasons.append("NO_DATA_ROWS")
    cells = [cell for row in data_rows for cell in row if not cell.empty]
    total = sum(len(row) for row in data_rows) or 1
    if len(cells) / total < float(min_filled):
        reasons.append("SPARSE_CELLS")
    # Merged cells are normal in Chinese reports and are absorbed by the binder
    # (grade columns are aligned from the right edge), so spans are reported as a
    # warning instead of rejecting an otherwise usable table.
    warnings = []
    if any(cell.rowspan > 1 or cell.colspan > 1 for row in table.rows for cell in row):
        warnings.append("HAS_SPANS")
    return {"status": "table_verified" if not reasons else "table_unverified",
            "reasons": reasons, "warnings": warnings,
            "header_paths": [list(path) for path in paths],
            "data_rows": len(data_rows), "filled_ratio": round(len(cells) / total, 3)}


class VisionChannel:
    """Bounded client for an OpenAI-compatible vision endpoint."""

    def __init__(self, *, base_url: str | None = None, model: str | None = None,
                 api_key_env: str = "COMPLIANCE_VISION_API_KEY", concurrency: int = 2,
                 timeout: float = 180.0, transport=None, retries: int = 1,
                 deadline_seconds: float = 60.0, max_tokens: int = 1500,
                 stream: bool = True):
        base = base_url or os.environ.get("COMPLIANCE_VISION_BASE_URL") or DEFAULT_BASE
        if not base.startswith(("http://", "https://")):
            raise ValueError("vision base_url must be http(s)")
        self.base_url = base.rstrip("/")
        self.model = model or os.environ.get("COMPLIANCE_VISION_MODEL") or DEFAULT_MODEL
        self.api_key = os.environ.get(api_key_env, "")
        self.timeout = float(timeout)
        self.deadline_seconds = float(deadline_seconds)
        self.max_tokens = max(256, min(4096, int(max_tokens)))
        self.stream = bool(stream)
        self.transport = transport
        self.retries = max(0, int(retries))
        self._semaphore = BoundedSemaphore(max(1, int(concurrency)))
        self._lock = Lock()
        self._calls = 0
        self._failures = 0

    def stats(self) -> dict:
        with self._lock:
            return {"calls": self._calls, "failures": self._failures}

    def complete(self, image: bytes, prompt: str) -> str:
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise ValueError("image bytes are required")
        if not self.api_key:
            raise VisionUnavailable("vision api key is not configured")
        payload = build_payload(self.model, image, prompt, max_tokens=self.max_tokens,
                                stream=self.stream and self.transport is None)
        last = None
        for attempt in range(self.retries + 1):
            with self._semaphore:
                with self._lock:
                    self._calls += 1
                try:
                    if payload.get("stream"):
                        return self._send_streaming(payload, self.deadline_seconds)
                    return self._send(payload)
                except VisionDeadlineExceeded:
                    # A deadline is terminal for this call: do not retry it.
                    with self._lock:
                        self._failures += 1
                    raise
                except VisionUnavailable as exc:
                    last = str(exc)
                    with self._lock:
                        self._failures += 1
            if attempt < self.retries:
                continue
        raise VisionUnavailable(last or "unavailable")

    def _send_streaming(self, payload: dict, deadline_seconds: float) -> str:
        """Stream the answer and close the socket when the deadline passes.

        A socket timeout only bounds one read; this bounds the whole answer, so a
        slow model can never pin a worker or keep consuming a shared endpoint.
        """
        import http.client
        from time import monotonic
        from urllib.parse import urlsplit

        parts = urlsplit(self.base_url)
        path = (parts.path or "") + "/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json", "Accept": "text/event-stream"}
        deadline = monotonic() + float(deadline_seconds)
        connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parts.hostname, parts.port, timeout=min(30.0, float(deadline_seconds)))
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise VisionUnavailable("http%d" % response.status)
            lines = []
            while True:
                if monotonic() > deadline:
                    raise VisionDeadlineExceeded("vision deadline exceeded")
                line = response.readline()
                if not line:
                    break
                lines.append(line)
                if line.strip() == b"data: [DONE]":
                    break
            text = parse_sse(lines)
            if not text:
                raise VisionUnavailable("empty_stream")
            return text
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _send(self, payload: dict) -> str:
        url = self.base_url + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json", "Accept": "application/json"}
        body = json.dumps(payload).encode("utf-8")
        if self.transport is None:
            request = urlrequest.Request(url, data=body, headers=headers, method="POST")
            try:
                with urlrequest.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    raw = response.read()
            except Exception as exc:
                raise VisionUnavailable(type(exc).__name__) from exc
        else:
            try:
                raw = self.transport(url, headers, body, self.timeout)
            except Exception as exc:                      # injected transport failure
                raise VisionUnavailable(type(exc).__name__) from exc
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            return payload["choices"][0]["message"]["content"]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise VisionUnavailable("invalid vision response") from exc

    def transcribe_table(self, image: bytes, *, file_id: str, page: int,
                         file_hash: str | None = None) -> tuple:
        """Ask for an HTML table and run the gates; returns (Table|None, diagnostics)."""
        content = self.complete(image, TABLE_PROMPT)
        html = extract_html(content)
        if not html:
            return None, {"status": "table_rejected", "reasons": ["NO_HTML"]}
        tables = tables_from_html([{"label": "table", "content": html}], file_id=file_id,
                                  page=page, file_hash=file_hash)
        if not tables:
            return None, {"status": "table_rejected", "reasons": ["UNPARSABLE_HTML"]}
        gate = validate_table(tables[0])
        if gate["status"] != "table_verified":
            return None, gate
        return tables[0], gate
