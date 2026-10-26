import io
import json
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError, URLError
from email.message import Message

from compliance_service.callbacks import (
    CallbackPermanentError,
    CallbackRetryableError,
    HttpCallbackClient,
    URLPolicy,
    map_callback,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.headers = Message()

    def getcode(self):
        return self._status

    def read(self, _limit=-1):
        return self._payload

    def close(self):
        pass


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.request = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


class FailingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout):
        raise self.error


class CallbackTests(unittest.TestCase):
    @staticmethod
    def resolver(*args, **kwargs):
        return [(2, 1, 6, "", ("93.184.215.14", 443))]

    def test_map_preserves_mine_and_cycle_and_omits_local_attachment(self):
        run = {"spec": {"mine_code": "001", "item_code": "stop_work_unload", "check_cycle": "2026-10"}}
        result = {"status": "pending", "description": "待核验", "detail": "部分资料待核验",
                  "attachments": [{"file_id": "local", "attachmentContent": "问题"}]}
        snapshot = {"files": [{"file_id": "local", "source_path": "C:/x.pdf", "original_name": "日报.pdf"}]}
        payload = map_callback(run, result, snapshot, "2026-10-25T12:00:00+08:00")
        self.assertEqual(payload["mineId"], "001")
        self.assertEqual(payload["checkCycle"], "2026-10")
        self.assertEqual(payload["attachments"], [])

    def test_map_includes_only_explicit_external_attachment_content(self):
        run = {"spec": {"mine_code": "m", "item_code": "relief_effects", "check_cycle": "2026-10"}}
        result = {"status": "no_pass", "description": "不通过", "detail": "证据证明未满足",
                  "findings": [{"evidence_refs": ["e"], "description": "第2页缺少签署"}], "attachments": [{"file_id": "f", "attachmentContent": "第2页缺少签署"}]}
        snapshot = {"files": [{"file_id": "f", "original_name": "验收.pdf",
                                "content_hash": "hash", "source_metadata": {"attachment_url": "https://files.example/evidence.pdf"}}],
                    "evidence": [{"evidence_id": "e", "file_id": "f", "locator": {"page": 2}}]}
        payload = map_callback(run, result, snapshot, "2026-10-25T12:00:00+08:00")
        self.assertEqual(payload["attachments"][0]["attachmentUrl"], "https://files.example/evidence.pdf")
        self.assertEqual(payload["attachments"][0]["attachmentContent"], "第2页缺少签署")

    def test_policy_requires_allowlist_and_denies_private(self):
        with self.assertRaises(ValueError):
            URLPolicy()
        policy = URLPolicy(allowed_hosts=frozenset({"127.0.0.1"}), allow_http=True, allow_loopback=True)
        self.assertEqual(policy.validate("http://127.0.0.1:8080/callback"), "http://127.0.0.1:8080/callback")
        with self.assertRaises(CallbackPermanentError):
            policy.validate("http://127.0.0.1:8080/callback#x")
        with self.assertRaises(CallbackPermanentError):
            policy.validate("http://user:pass@127.0.0.1:8080/callback")

    def test_client_sends_idempotency_and_requires_business_success(self):
        opener = FakeOpener(FakeResponse(b'{"code":200,"success":true}'))
        client = HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})), opener=opener, resolver=self.resolver)
        result = client.send("https://example.com/callback", {"x": 1}, "delivery-1")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(opener.request.get_header("Idempotency-key"), "delivery-1")
        self.assertEqual(json.loads(opener.request.data), {"x": 1})

        bad = HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})),
                                 opener=FakeOpener(FakeResponse(b'{"code":200,"success":false}')), resolver=self.resolver)
        with self.assertRaises(CallbackPermanentError):
            bad.send("https://example.com/callback", {}, "delivery-2")

    def test_callback_auth_headers_are_sent_outside_payload_and_validated(self):
        opener = FakeOpener(FakeResponse(b'{"code":200,"success":true}'))
        client = HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})),
                                    opener=opener, resolver=self.resolver,
                                    extra_headers={"Authorization": "Bearer test-token", "X-Source": "inspection"})
        payload = {"mineId": "01"}
        client.send("https://example.com/callback", payload, "stable-id")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(opener.request.data), payload)
        self.assertNotIn("Authorization", payload)
        for headers in ({"Content-Type": "text/plain"}, {"Idempotency-Key": "other"},
                        {"Host": "other"}, {"Content-Length": "1"},
                        {"X Name": "value"}, {"X-Name": "bad\r\nInjected: value"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})), extra_headers=headers)

    def test_callback_description_context_added_only_once(self):
        run = {"spec": {"mine_code": "001", "item_code": "stop_work_unload", "check_cycle": "2026-10"}}
        result = {"status": "pending", "description": "待核验", "detail": "缺证"}
        mapped = map_callback(run, result, None, "2026-10-25T12:00:00+08:00")
        self.assertEqual(mapped["description"], "矿井001 检查周期2026-10：待核验")
        result["description"] = mapped["description"]
        self.assertEqual(map_callback(run, result, None, "2026-10-25T12:00:00+08:00")["description"], mapped["description"])

    def test_client_rejects_oversized_response(self):
        opener = FakeOpener(FakeResponse(b"x" * 11))
        client = HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})),
                                    opener=opener, max_response_bytes=10, resolver=self.resolver)
        with self.assertRaises(CallbackPermanentError):
            client.send("https://example.com/callback", {}, "delivery-3")

    def test_client_denies_allowlisted_host_resolving_to_metadata(self):
        opener = FakeOpener(FakeResponse(b'{"code":200,"success":true}'))
        client = HttpCallbackClient(URLPolicy(allowed_hosts=frozenset({"example.com"})), opener=opener,
                                    resolver=lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 443))])
        with self.assertRaises(CallbackPermanentError):
            client.send("https://example.com/callback", {}, "delivery-4")
        self.assertIsNone(opener.request)

    def test_client_classifies_errors_and_malformed_body(self):
        policy = URLPolicy(allowed_hosts=frozenset({"example.com"}))
        cases = [(HTTPError("https://example.com", 503, "unavailable", {}, None), CallbackRetryableError),
                 (HTTPError("https://example.com", 401, "denied", {}, None), CallbackPermanentError),
                 (TimeoutError(), CallbackRetryableError)]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = HttpCallbackClient(policy, opener=FailingOpener(error), resolver=self.resolver)
                with self.assertRaises(expected):
                    client.send("https://example.com/callback", {}, "delivery")
        for body in (b"[]", b"false", b"bad-json", b'{"code":200,"success":false}'):
            with self.subTest(body=body):
                client = HttpCallbackClient(policy, opener=FakeOpener(FakeResponse(body)), resolver=self.resolver)
                with self.assertRaises(CallbackPermanentError):
                    client.send("https://example.com/callback", {}, "delivery")
        response = FakeResponse(b'{"code":200,"success":true}')
        response.headers["Content-Length"] = "invalid"
        with self.assertRaises(CallbackPermanentError):
            HttpCallbackClient(policy, opener=FakeOpener(response), resolver=self.resolver).send("https://example.com/callback", {}, "delivery")

    def test_real_http_success_and_redirect_are_distinct(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append((self.path, self.headers.get("Idempotency-Key"), json.loads(data)))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/received")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":200,"success":true}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            client = HttpCallbackClient(URLPolicy(allowed_origins=frozenset({origin}), allow_http=True, allow_loopback=True))
            client.send(origin + "/received", {"x": "一致"}, "same-delivery")
            with self.assertRaises(CallbackPermanentError):
                client.send(origin + "/redirect", {"x": "一致"}, "same-delivery")
            self.assertEqual(seen, [("/received", "same-delivery", {"x": "一致"}),
                                    ("/redirect", "same-delivery", {"x": "一致"})])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_mapper_does_not_promote_global_refs_to_attachment_problem(self):
        run = {"spec": {"mine_code": "01", "item_code": "stop_work_unload", "check_cycle": "2026-10"}}
        result = {"status": "pending", "description": "待核验", "detail": "未闭合",
                  "evidence_refs": ["e"], "attachments": [{"file_id": "f", "attachmentContent": "不能直接当问题"}]}
        snapshot = {"files": [{"file_id": "f", "original_name": "C:/local/日报.pdf",
                                "source_metadata": {"original_url": "https://files.example/e.pdf", "attachment_cycle": "2026-09"}}],
                    "evidence": [{"file_id": "f", "evidence_id": "e"}]}
        attachment = map_callback(run, result, snapshot, "2026-10-25T12:00:00+08:00")["attachments"][0]
        self.assertNotIn("attachmentContent", attachment)
        self.assertEqual(attachment["attachmentName"], "日报.pdf")
        self.assertEqual(attachment["attachmentCycle"], "2026-09")


if __name__ == "__main__":
    unittest.main()
