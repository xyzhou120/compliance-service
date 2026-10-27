"""Bounded vision requests: small payloads, SSE parsing and a hard deadline."""

import json
import unittest
from time import monotonic

from compliance_service.vlm import (VisionChannel, VisionDeadlineExceeded, build_payload,
                                    parse_sse)


class PayloadTests(unittest.TestCase):
    def test_payload_is_small_and_thinking_is_off(self):
        payload = build_payload("m", b"img", "p", max_tokens=1500, enable_thinking=False,
                                stream=True)
        self.assertEqual(1500, payload["max_tokens"])
        self.assertFalse(payload["enable_thinking"])
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(payload["stream"])
        self.assertEqual(0, payload["temperature"])

    def test_output_budget_is_clamped(self):
        channel = VisionChannel(api_key_env="PATH", max_tokens=99999)
        self.assertLessEqual(channel.max_tokens, 4096)
        small = VisionChannel(api_key_env="PATH", max_tokens=1)
        self.assertGreaterEqual(small.max_tokens, 256)


class SseTests(unittest.TestCase):
    def test_deltas_are_joined_and_noise_ignored(self):
        lines = [
            b": keep-alive\n",
            b'data: {"choices":[{"delta":{"content":"<table>"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"<tr><td>a</td></tr>"}}]}\n',
            b"data: [DONE]\n",
        ]
        self.assertEqual("<table><tr><td>a</td></tr>", parse_sse(lines))
        self.assertEqual("", parse_sse([b"data: {bad json}\n"]))


class DeadlineTests(unittest.TestCase):
    def test_deadline_is_enforced_and_surfaces_as_unavailable(self):
        channel = VisionChannel(api_key_env="PATH", deadline_seconds=0.01)

        def slow(payload, deadline):
            now = monotonic()
            while monotonic() - now < 0.05:
                pass
            raise VisionDeadlineExceeded("vision deadline exceeded")

        channel._send_streaming = slow
        channel.retries = 0
        with self.assertRaises(VisionDeadlineExceeded):
            channel.complete(b"img", "x")

    def test_streaming_is_used_only_for_real_http(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append(url)
            return json.dumps({"choices": [{"message": {"content": "<table></table>"}}]}).encode()

        channel = VisionChannel(transport=transport, api_key_env="PATH")
        self.assertEqual("<table></table>", channel.complete(b"img", "x"))
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
