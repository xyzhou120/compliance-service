"""Bounded client for the private layout / OCR / vision endpoints.

Two shared services are used by several teams, so this client is deliberately
conservative: at most a handful of in-flight requests, bounded retries, a circuit
breaker, and explicit timeouts. It never decides anything about compliance.
"""

from __future__ import annotations

import json
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib import request as urlrequest
from uuid import uuid4

LAYOUT_PATH = "/layout_detection"
VL_OCR_PATH = "/vl_ocr"
VL_OCR_NO_LAYOUT_PATH = "/vl_ocr_no_layout"
OCR_PATH = "/ocr"
TABLE_CELL_PATH = "/table_cell_detection"


class ChannelUnavailable(RuntimeError):
    """The shared service refused or failed; the page must be retried later."""


def multipart_body(image: bytes, *, field: str = "file", filename: str = "page.png") -> tuple[bytes, str]:
    """Build a multipart/form-data body without third-party dependencies."""
    boundary = "----compliance" + uuid4().hex
    head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: image/png\r\n\r\n" % (boundary, field, filename)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return head + image + tail, "multipart/form-data; boundary=" + boundary


class RecognitionChannels:
    """Serialised, retrying access to one shared recognition service."""

    def __init__(self, base_url: str, *, concurrency: int = 2, timeout: float = 90.0,
                 transport=None, retries: int = 1, backoff: float = 1.5,
                 failure_threshold: int = 3, cooldown_seconds: float = 30.0,
                 clock=monotonic, sleep=None):
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) endpoint")
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 8:
            raise ValueError("concurrency must be between 1 and 8")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.transport = transport
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._sleep = sleep or (lambda seconds: None)
        self._semaphore = BoundedSemaphore(concurrency)
        self._lock = Lock()
        self._failures = 0
        self._open_until = 0.0
        self._stats = {"calls": 0, "failures": 0, "retries": 0, "rejections": 0}

    # -- public channel helpers ------------------------------------------------
    def layout(self, image: bytes, *, timeout: float | None = None) -> list:
        return self._require_list(self._call(LAYOUT_PATH, image, timeout), LAYOUT_PATH)

    def vl_ocr(self, image: bytes, *, timeout: float | None = None) -> list:
        return self._require_list(self._call(VL_OCR_PATH, image, timeout), VL_OCR_PATH)

    def vl_ocr_no_layout(self, image: bytes, *, timeout: float | None = None) -> list:
        """Layout-free VL variant, used when the layout pass yields almost no text."""
        return self._require_list(self._call(VL_OCR_NO_LAYOUT_PATH, image, timeout),
                                  VL_OCR_NO_LAYOUT_PATH)

    def table_cells(self, image: bytes, *, timeout: float | None = None) -> list:
        return self._require_list(self._call(TABLE_CELL_PATH, image, timeout), TABLE_CELL_PATH)

    def ocr(self, image: bytes, *, timeout: float | None = None) -> dict:
        payload = self._call(OCR_PATH, image, timeout)
        if not isinstance(payload, dict) or "rec_texts" not in payload:
            raise ChannelUnavailable("ocr response has no rec_texts")
        return payload

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _require_list(payload, path: str) -> list:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("blocks"), list):
            return payload["blocks"]
        raise ChannelUnavailable("unexpected response shape for " + path)

    def _circuit_open(self) -> bool:
        with self._lock:
            return self._open_until > self._clock()

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._stats["failures"] += 1
            if self._failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown_seconds
                self._stats["rejections"] += 1

    def _call(self, path: str, image: bytes, timeout: float | None):
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise ValueError("image bytes are required")
        if self._circuit_open():
            raise ChannelUnavailable("circuit_open:" + path)
        last_error: str | None = None
        for attempt in range(self.retries + 1):
            with self._semaphore:
                with self._lock:
                    self._stats["calls"] += 1
                try:
                    body, content_type = multipart_body(bytes(image))
                    payload = self._send(path, body, content_type, self.timeout if timeout is None else timeout)
                    self._record_success()
                    return payload
                except ChannelUnavailable as exc:
                    last_error = str(exc)
                except Exception as exc:                     # network / decoding
                    last_error = type(exc).__name__ + ":" + str(exc)[:120]
            self._record_failure()
            if attempt < self.retries:
                with self._lock:
                    self._stats["retries"] += 1
                self._sleep(self.backoff * (attempt + 1))
                if self._circuit_open():
                    break
        raise ChannelUnavailable(last_error or "unavailable")

    def _send(self, path: str, body: bytes, content_type: str, timeout: float):
        url = self.base_url + path
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if self.transport is None:
            request = urlrequest.Request(url, data=body, headers=headers, method="POST")
            try:
                with urlrequest.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured endpoint
                    raw = response.read()
            except Exception as exc:
                raise ChannelUnavailable(type(exc).__name__) from exc
        else:
            raw = self.transport(url, headers, body, timeout)
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ChannelUnavailable("invalid json from " + path) from exc
