from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from compliance_service.ocr_documents import parse_with_models, public_model_settings, recognize_checked


def settings():
    return {"ocr": {"enabled": True, "provider": "multipart-http", "base_url": "http://test/ocr-root"},
            "vision": {"enabled": True, "provider": "openai-compatible", "base_url": "http://test/v1", "model": "test-model", "api_key": "secret-not-for-provenance"}}


class RecognitionQualityTests(unittest.TestCase):
    def test_two_page_workers_preserve_evidence_order_and_bound_concurrency(self):
        from pypdf import PdfWriter
        from threading import Lock
        from time import sleep
        native = {"file_id":"report","format":"pdf","content_hash":"hash","blocks":[],"issues":[],
                  "coverage":[{"unit_id":f"page:{page}","state":"unreadable","reason":"page_image_requires_ocr"}
                              for page in (1,2,3)]}
        running = 0; peak = 0; lock = Lock()
        def fake_recognize(image_bytes, _mime, _settings, **_kwargs):
            nonlocal running, peak
            with lock:
                running += 1; peak = max(peak, running)
            sleep(0.03)
            with lock: running -= 1
            return {"readable": True, "issues": [], "candidates": [], "quality_policy": "test",
                    "checked_blocks": [{"text": image_bytes.decode(), "quality": "independently_compared_candidate",
                                        "source_refs": []}]}
        with TemporaryDirectory() as directory:
            path=Path(directory)/"report.pdf"; writer=PdfWriter()
            for _ in range(3): writer.add_blank_page(width=100,height=100)
            with path.open("wb") as stream: writer.write(stream)
            with patch("compliance_service.ocr_documents.parse_document",side_effect=lambda *args,**kwargs:__import__('copy').deepcopy(native)), \
                 patch("compliance_service.ocr_documents._pdf_image_candidates",side_effect=lambda _path,page,_reader:[(str(page).encode(),"image/png",{"page":page})]), \
                 patch("compliance_service.ocr_documents.recognize_checked",side_effect=fake_recognize):
                one=settings(); one["ocr"]["recognition_concurrency"]=1
                serial=parse_with_models(path,file_id="report",settings=one)
                two=settings(); two["ocr"]["recognition_concurrency"]=2
                concurrent=parse_with_models(path,file_id="report",settings=two)
        self.assertEqual(2,peak)
        self.assertEqual(serial["blocks"],concurrent["blocks"])
        self.assertEqual(serial["coverage"],concurrent["coverage"])
        self.assertEqual(serial["recognition_budget"],concurrent["recognition_budget"])

    def test_page_worker_error_does_not_cancel_sibling_pages(self):
        from pypdf import PdfWriter
        native={"file_id":"report","format":"pdf","content_hash":"hash","blocks":[],"issues":[],
                "coverage":[{"unit_id":f"page:{page}","state":"unreadable","reason":"page_image_requires_ocr"}
                            for page in (1,2)]}
        with TemporaryDirectory() as directory:
            path=Path(directory)/"report.pdf";writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.add_blank_page(width=100,height=100)
            with path.open("wb") as stream:writer.write(stream)
            def images(_path,page,_reader):
                if page==1: raise NotImplementedError("unsupported image filter")
                return [(b"page2","image/png",{"page":2})]
            with patch("compliance_service.ocr_documents.parse_document",return_value=native), \
                 patch("compliance_service.ocr_documents._pdf_image_candidates",side_effect=images), \
                 patch("compliance_service.ocr_documents._render_page",return_value=b"fallback"), \
                 patch("compliance_service.ocr_documents.recognize_checked",return_value={"readable":False,"issues":["independent_recognition_not_confirmed"],"candidates":[],"quality_policy":"test","checked_blocks":[]}):
                value=settings();value["ocr"]["recognition_concurrency"]=2
                result=parse_with_models(path,file_id="report",settings=value)
        self.assertEqual(2,len(result["coverage"]))
        self.assertEqual("incomplete",result["coverage"][0]["recognition"]["status"])
        self.assertEqual("incomplete",result["coverage"][1]["recognition"]["status"])
        self.assertNotIn("CancelledError",str(result))

    def test_recognition_budget_reports_skipped_pages(self):
        from pypdf import PdfWriter
        settings_value = settings(); settings_value["ocr"]["max_pages"] = 1
        native = {"file_id":"report","format":"pdf","content_hash":"hash","blocks":[],"issues":[],
                  "coverage":[{"unit_id":"page:1","state":"unreadable","reason":"page_image_requires_ocr"},
                               {"unit_id":"page:2","state":"unreadable","reason":"page_image_requires_ocr"}]}
        events=[]
        with TemporaryDirectory() as directory:
            path=Path(directory)/"report.pdf"; writer=PdfWriter()
            writer.add_blank_page(width=100,height=100);writer.add_blank_page(width=100,height=100)
            with path.open("wb") as stream: writer.write(stream)
            with patch("compliance_service.ocr_documents.parse_document",return_value=native), \
                 patch("compliance_service.ocr_documents._pdf_image_candidates",return_value=[]), \
                 patch("compliance_service.ocr_documents._render_page",return_value=b"image"):
                result=parse_with_models(path,file_id="report",settings=settings_value,transport=lambda *args:{"text":"x"},progress_callback=events.append)
        self.assertEqual({"eligible_pages":2,"attempted_pages":1,"skipped_pages":1,"max_pages":1},result["recognition_budget"])
        self.assertTrue(any(e.get("status")=="skipped" for e in events))

    def test_decompression_bomb_uses_page_render_fallback(self):
        from pypdf import PdfWriter
        native={"file_id":"report","format":"pdf","content_hash":"hash","blocks":[],"issues":[],
                "coverage":[{"unit_id":"page:1","state":"unreadable","reason":"page_image_requires_ocr"}]}
        with TemporaryDirectory() as directory:
            path=Path(directory)/"report.pdf"; writer=PdfWriter();writer.add_blank_page(width=100,height=100)
            with path.open("wb") as stream: writer.write(stream)
            with patch("compliance_service.ocr_documents.parse_document",return_value=native), \
                 patch("compliance_service.ocr_documents._pdf_image_candidates",side_effect=__import__('PIL').Image.DecompressionBombError("too large")), \
                 patch("compliance_service.ocr_documents._render_page",return_value=b"image"):
                result=parse_with_models(path,file_id="report",settings=settings(),transport=lambda *args:{"text":"x"})
        self.assertEqual("incomplete",result["coverage"][0]["recognition"]["status"])
        self.assertIn("embedded_image_limit",result["coverage"][0]["recognition"]["issues"])
    def test_oversized_pdf_image_is_a_page_gap_and_does_not_abort_document(self):
        from io import BytesIO
        from pypdf import PdfWriter
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with path.open("wb") as stream:
                writer.write(stream)
            native = {"file_id": "report", "format": "pdf", "content_hash": "hash",
                      "blocks": [], "issues": [], "coverage": [
                          {"unit_id": "page:1", "state": "unreadable", "reason": "page_image_requires_ocr"},
                          {"unit_id": "page:2", "state": "unreadable", "reason": "page_image_requires_ocr"}]}
            events = []
            image = Image.new("RGB", (2, 2), "white")
            data = BytesIO(); image.save(data, format="PNG")
            def image_for_page(_path, page, _reader):
                if page == 1:
                    raise Image.DecompressionBombError("image exceeds Pillow's pixel limit")
                return [(data.getvalue(), "image/png", {"page": page})]
            with patch("compliance_service.ocr_documents.parse_document", return_value=native), \
                 patch("compliance_service.ocr_documents._pdf_image_candidates", side_effect=image_for_page), \
                 patch("compliance_service.ocr_documents._render_page", side_effect=RuntimeError("render failed")):
                result = parse_with_models(path, file_id="report", settings=settings(),
                                           transport=lambda *args: {"text": "test"}, progress_callback=events.append)
            self.assertEqual("failed", result["coverage"][0]["recognition"]["status"])
            self.assertIn("recognition_failed:RuntimeError", result["coverage"][0]["recognition"]["issues"])
            self.assertIn("recognition", result["coverage"][1])
            failed = [e for e in events if e.get("event") == "page_finished" and e.get("status") == "failed"]
            self.assertEqual([1], [e["page"] for e in failed])

    def test_line_order_changes_keep_source_refs_without_whole_page_equality(self):
        def transport(url, headers, payload, timeout):
            if url.endswith("/ocr"):
                return {"rec_texts": ["监测值8MPa", "阈值10MPa"]}
            return {"choices": [{"message": {"content": '{"text":"阈值 10MPa\\n监测值 8MPa"}'}}]}
        result = recognize_checked(b"image", "image/png", settings(), transport=transport)
        self.assertTrue(result["readable"])
        self.assertEqual(len(result["checked_blocks"]), 2)
        self.assertEqual(result["checked_blocks"][0]["source_refs"][1]["block"], 1)

    def test_partial_matching_retains_checked_blocks_and_unmatched_candidates(self):
        def transport(url, headers, payload, timeout):
            if url.endswith("/ocr"):
                return {"rec_texts": ["工作面W1", "监测值8MPa"]}
            return {"choices": [{"message": {"content": '{"text":"工作面W1\\n监测值3MPa"}'}}]}
        result = recognize_checked(b"image", "image/png", settings(), transport=transport)
        self.assertFalse(result["readable"])
        self.assertEqual(result["text"], "工作面W1")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("independent_recognition_not_confirmed", result["issues"])

    def test_progress_contains_timing_without_provider_secrets(self):
        events = []
        recognize_checked(b"image", "image/png", settings(),
                          transport=lambda *args: {"text": "原文"},
                          file_id="F1", page=2, progress_callback=events.append)
        completed = [event for event in events if event["event"] == "request_finished"]
        self.assertEqual(len(completed), 2)
        self.assertTrue(all("elapsed_ms" in event for event in completed))
        self.assertNotIn("secret-not-for-provenance", str(events))

    def test_independent_normalized_transcripts_are_checked_not_guessed(self):
        def transport(url, headers, payload, timeout):
            if url.endswith("/ocr"):
                return {"rec_texts": ["监测值8MPa", "阈值10MPa"], "rec_scores": [.99, .99]}
            return {"choices": [{"message": {"content": '{"text":"监测值 8 MPa\\n阈值 10 MPa","blocks":[]}'}}]}
        result = recognize_checked(b"image", "image/png", settings(), transport=transport)
        self.assertTrue(result["readable"], result)
        self.assertEqual(2, len(result["candidates"]))

    def test_numeric_disagreement_or_single_provider_remains_unreadable(self):
        def transport(url, headers, payload, timeout):
            if url.endswith("/ocr"):
                return {"rec_texts": ["监测值8MPa"]}
            return {"choices": [{"message": {"content": '{"text":"监测值3MPa","blocks":[]}'}}]}
        result = recognize_checked(b"image", "image/png", settings(), transport=transport)
        self.assertFalse(result["readable"])
        self.assertEqual("", result["text"])
        only = settings()
        only["vision"]["enabled"] = False
        self.assertFalse(recognize_checked(b"image", "image/png", only, transport=transport)["readable"])

    def test_punctuation_normalization_cannot_erase_decimal_sign_or_unit_case(self):
        for ocr_text, vision_text in (("测值8.0MPa", "测值80MPa"), ("测值-8MPa", "测值8MPa"),
                                      ("测值8mV", "测值8MV")):
            def transport(url, headers, payload, timeout):
                if url.endswith("/ocr"):
                    return {"rec_texts": [ocr_text]}
                import json
                return {"choices": [{"message": {"content": json.dumps({"text": vision_text})}}]}
            self.assertFalse(recognize_checked(b"image", "image/png", settings(), transport=transport)["readable"])

    def test_image_coverage_and_original_hash_are_preserved(self):
        def transport(url, headers, payload, timeout):
            if url.endswith("/ocr"):
                return {"rec_texts": ["W1 value8"]}
            return {"choices": [{"message": {"content": '{"text":"W1 value8","blocks":[]}'}}]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scan.png"
            Image.new("RGB", (30, 30), "white").save(path)
            before = path.read_bytes()
            result = parse_with_models(path, file_id="scan", settings=settings(), transport=transport)
            self.assertEqual("readable", result["coverage"][0]["state"])
            self.assertEqual("recognized_page", result["blocks"][0]["kind"])
            self.assertEqual(before, path.read_bytes())
            self.assertEqual({"image": 1}, result["blocks"][0]["locator"])
            self.assertNotIn("recognition_candidates", result["blocks"][0]["metadata"])
            self.assertEqual(2, len(result["coverage"][0]["recognition"]["images"][0]["recognition_candidates"]))
            self.assertNotIn("secret-not-for-provenance", str(result))

    def test_disabled_settings_open_no_socket_and_do_not_expose_secret(self):
        disabled = settings()
        disabled["ocr"]["enabled"] = disabled["vision"]["enabled"] = False
        with patch("socket.socket", side_effect=AssertionError("disabled providers cannot call network")):
            result = recognize_checked(b"image", "image/png", disabled)
        self.assertFalse(result["readable"])
        self.assertNotIn("secret-not-for-provenance", str(public_model_settings(settings())))
