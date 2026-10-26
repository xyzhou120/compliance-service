import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compliance_service.config import load_model_settings
from compliance_service.providers import recognize_image


class ProviderTests(unittest.TestCase):
    def test_missing_configuration_is_disabled_without_transport(self):
        called = []
        result = recognize_image(b"x", "image/png", {"ocr": {}, "vision": {}}, lambda *args: called.append(args))
        self.assertIn("provider_disabled", result["issues"])
        self.assertFalse(called)

    def test_settings_load_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text('[ocr]\nenabled=true\nurl="http://ocr"\n', encoding="utf-8")
            settings = load_model_settings(path)
        self.assertTrue(settings["ocr"]["enabled"])
        self.assertFalse(settings["vision"]["enabled"])

    def test_ocr_transport_and_secret_not_in_result(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
            return json.dumps({"text": "压力 12", "blocks": [{"text": "压力 12", "bbox": [1, 2, 3, 4]}]})

        with patch.dict(os.environ, {"TEST_OCR_KEY": "secret-value"}):
            result = recognize_image(b"abc", "image/png", {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr", "api_key_env": "TEST_OCR_KEY"}}, transport)
        self.assertEqual(result["text"], "压力 12")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-value")
        self.assertNotIn("secret-value", repr(result))
        self.assertEqual(captured["payload"]["mime_type"], "image/png")

    def test_multi_number_page_is_not_a_conflict(self):
        result = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr"}},
                                 lambda *args: {"text": "甲8MPa，乙10MPa", "blocks": [{"text": "甲8MPa"}, {"text": "乙10MPa"}]})
        self.assertNotIn("numeric_candidates_conflict", result["issues"])

    def test_multipart_uses_file_and_image_extension(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured.update(url=url, headers=headers, payload=payload)
            return {"text": "可读"}

        result = recognize_image(b"abc", "image/png", {"ocr": {"enabled": True, "provider": "multipart-http", "base_url": "http://ocr"}}, transport)
        self.assertEqual(captured["url"], "http://ocr/ocr")
        self.assertIn(b'name="file"; filename="image.png"', captured["payload"])
        self.assertTrue(captured["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertFalse(result["issues"])

    def test_multipart_real_ocr_rec_texts_response(self):
        response = {"rec_texts": ["工作面 W1", "监测值8MPa", "阈值10MPa"], "rec_scores": [0.9345, 0.9956, 0.9989],
                    "rec_polys": [[[0, 0], [10, 0], [10, 10], [0, 10]]] * 3}
        # Separate observations may share coordinates only in synthetic data;
        # use distinct real line boxes here.
        response["rec_polys"] = [[[0, y], [10, y], [10, y + 10], [0, y + 10]] for y in (0, 20, 40)]
        result = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "multipart-http", "base_url": "http://ocr"}}, lambda *args: response)
        self.assertEqual(result["text"], "工作面 W1\n监测值8MPa\n阈值10MPa")
        self.assertEqual(result["blocks"][1]["confidence"], 0.9956)
        self.assertEqual(result["quality"], "candidate")
        self.assertFalse(result["issues"])

    def test_ocr_metadata_length_mismatch_is_explicit(self):
        result = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "multipart-http", "base_url": "http://ocr"}},
                                 lambda *args: {"rec_texts": ["监测值8MPa", "阈值10MPa"], "rec_scores": [0.99], "rec_polys": []})
        self.assertEqual(len(result["blocks"]), 2)
        self.assertIn("ocr_score_count_mismatch", result["issues"])
        self.assertIn("ocr_polygon_count_mismatch", result["issues"])
        self.assertNotIn("confidence", result["blocks"][0])

    def test_local_key_fallback_and_env_priority(self):
        settings = {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr", "api_key_env": "MODEL_TEST_KEY", "api_key": "local-key"}}
        headers = []

        def transport(url, given_headers, payload, timeout):
            headers.append(given_headers)
            return {"text": "可读"}

        with patch.dict(os.environ, {}, clear=True):
            recognize_image(b"x", "image/png", settings, transport)
        with patch.dict(os.environ, {"MODEL_TEST_KEY": "env-key"}):
            recognize_image(b"x", "image/png", settings, transport)
        self.assertEqual(headers[0]["Authorization"], "Bearer local-key")
        self.assertEqual(headers[1]["Authorization"], "Bearer env-key")

    def test_invalid_timeout_does_not_request(self):
        calls = []
        result = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr", "timeout_seconds": "bad"}}, lambda *args: calls.append(args))
        self.assertIn("invalid_timeout", result["issues"])
        self.assertFalse(calls)

    def test_plain_vision_text_is_not_accepted_as_json(self):
        result = recognize_image(b"x", "image/png", {"vision": {"enabled": True, "provider": "openai-compatible", "base_url": "http://vision/v1", "model": "model"}}, lambda *args: {"choices": [{"message": {"content": "Result PASS"}}]})
        self.assertIn("invalid_json", result["issues"])
        self.assertEqual(result["text"], "")

    def test_bad_json_and_numeric_conflict_remain_candidates_with_issues(self):
        bad = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr"}}, lambda *args: "{")
        self.assertIn("invalid_json", bad["issues"])
        conflict = recognize_image(b"x", "image/png", {"ocr": {"enabled": True, "provider": "http-json", "url": "http://ocr"}}, lambda *args: {"text": "12", "blocks": [{"text": "13"}]})
        self.assertIn("numeric_candidates_conflict", conflict["issues"])
        self.assertEqual(conflict["quality"], "candidate")

    def test_vision_uses_single_v1_path_and_openai_payload(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured.update(url=url, payload=payload)
            return {"choices": [{"message": {"content": '{"text":"visible","blocks":[]}'}}]}

        result = recognize_image(b"x", "image/jpeg", {"vision": {"enabled": True, "provider": "openai-compatible", "base_url": "http://vision/v1", "model": "v1-model"}}, transport)
        self.assertEqual(captured["url"], "http://vision/v1/chat/completions")
        self.assertEqual(result["model"], "v1-model")
        self.assertEqual(result["text"], "visible")


if __name__ == "__main__":
    unittest.main()
