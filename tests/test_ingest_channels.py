"""Bounded recognition client: retries, circuit breaker and concurrency limits."""

import json
import threading
import time
import unittest

from compliance_service.ingest import ChannelUnavailable, RecognitionChannels, multipart_body


def json_transport(payloads):
    """Return a transport that answers with the given payload per call order."""
    calls = []

    def transport(url, headers, body, timeout):
        calls.append(url)
        value = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode("utf-8")

    transport.calls = calls
    return transport


class ChannelTests(unittest.TestCase):
    def test_multipart_body_wraps_bytes_without_dependency(self):
        body, content_type = multipart_body(b"abc")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b"abc", body)
        self.assertIn(b"name=\"file\"", body)

    def test_each_channel_validates_its_response_shape(self):
        client = RecognitionChannels("http://ocr.test", transport=json_transport([[{"label": "chart"}]]))
        self.assertEqual([{"label": "chart"}], client.layout(b"img"))
        with self.assertRaises(ChannelUnavailable):
            client.ocr(b"img")                     # 缺少 rec_texts
        client = RecognitionChannels("http://ocr.test", transport=json_transport([{"rec_texts": ["a"]}]))
        self.assertEqual({"rec_texts": ["a"]}, client.ocr(b"img"))

    def test_retries_are_bounded_and_counted(self):
        transport = json_transport([ConnectionResetError("boom"), ConnectionResetError("boom"),
                                    [{"label": "table"}]])
        client = RecognitionChannels("http://ocr.test", transport=transport, retries=2, backoff=0)
        self.assertEqual([{"label": "table"}], client.layout(b"img"))
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(2, client.stats()["retries"])

    def test_persistent_failure_raises_and_opens_the_circuit(self):
        now = [1000.0]
        transport = json_transport([ConnectionResetError("down")])
        client = RecognitionChannels("http://ocr.test", transport=transport, retries=0,
                                     failure_threshold=2, cooldown_seconds=30, clock=lambda: now[0])
        with self.assertRaises(ChannelUnavailable):
            client.layout(b"img")
        with self.assertRaises(ChannelUnavailable):
            client.layout(b"img")
        calls_before = len(transport.calls)
        with self.assertRaises(ChannelUnavailable) as ctx:
            client.layout(b"img")
        self.assertIn("circuit_open", str(ctx.exception))
        self.assertEqual(calls_before, len(transport.calls))     # 熔断后不再打服务
        now[0] += 31
        ok = json_transport([[{"label": "text"}]])
        client.transport = ok
        self.assertEqual([{"label": "text"}], client.layout(b"img"))
        self.assertEqual(1, len(ok.calls))

    def test_concurrency_never_exceeds_the_configured_limit(self):
        state = {"running": 0, "peak": 0}
        lock = threading.Lock()

        def transport(url, headers, body, timeout):
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.05)
            with lock:
                state["running"] -= 1
            return json.dumps([{"label": "text"}]).encode("utf-8")

        client = RecognitionChannels("http://ocr.test", transport=transport, concurrency=2)
        threads = [threading.Thread(target=client.layout, args=(b"img",)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLessEqual(state["peak"], 2)
        self.assertEqual(6, client.stats()["calls"])

    def test_rejects_bad_configuration(self):
        with self.assertRaises(ValueError):
            RecognitionChannels("ftp://ocr.test")
        with self.assertRaises(ValueError):
            RecognitionChannels("http://ocr.test", concurrency=0)
        with self.assertRaises(ValueError):
            RecognitionChannels("http://ocr.test").layout(b"")


if __name__ == "__main__":
    unittest.main()
