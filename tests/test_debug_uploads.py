from base64 import b64encode
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.contracts import digest
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService
from compliance_service import debug_uploads


def upload(data=b"daily record", **overrides):
    return {"group_id": "prevention_daily", "kind": "file", "name": "日报.xlsx",
            "data": b64encode(data).decode(), **overrides}


def body(**overrides):
    return {"sourceSystem": "debug-documents", "mineCode": "mine-a", "environment": "local",
            "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}],
            "period": {"start": "2026-10-01", "end": "2026-10-31"}, "source_complete": False,
            "attachments": [upload()], **overrides}


class DownloadResponse(BytesIO):
    status = 200


class DebugBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = InspectionService(Ledger(self.root / "ledger.sqlite3"))
        self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)

    def post(self, value=None):
        return self.client.post("/v1/debug/bundles", json=body() if value is None else value)

    def assert_rejected(self, request):
        response = self.post(request)
        self.assertEqual(422, response.status_code, response.text)
        self.assertIsNone(self.service.ledger.claim("test"))
        self.assertEqual([], list((self.root / "uploads").glob("*/*/manifest.json")))
        return response

    def test_material_groups_are_stable_and_do_not_require_all_groups(self):
        groups = self.client.get("/v1/debug/material-groups").json()["groups"]
        self.assertEqual(10, len(groups))
        self.assertEqual({"requirement", "actual_record"}, {group["role"] for group in groups})
        self.assertTrue(all(group["items"] for group in groups))
        response = self.post()
        self.assertEqual(202, response.status_code, response.text)

    def test_multiple_same_name_daily_files_remain_distinct_and_both_items_use_bundle(self):
        response = self.post(body(attachments=[upload(b"one"), upload(b"two")],
                                  items=[{"itemCode": "stop_work_unload"}, {"itemCode": "relief_effects"}]))
        self.assertEqual(202, response.status_code, response.text)
        result = response.json()
        self.assertEqual(2, len(result["items"]))
        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([b"one", b"two"], [(manifest_path.parent / item["path"]).read_bytes() for item in manifest["files"]])
        self.assertEqual(2, len({item["file_id"] for item in manifest["files"]}))
        self.assertEqual(["日报.xlsx", "日报.xlsx"], [item["original_name"] for item in manifest["files"]])
        self.assertEqual(["prevention_daily", "prevention_daily"], [item["material_group"] for item in manifest["files"]])
        self.assertEqual(["日报.xlsx", "日报.xlsx"], [item["name"] for item in result["attachments"]])
        for accepted in result["items"]:
            run = self.service.ledger.get(accepted["run_id"], "debug-documents", "mine-a", "local")
            self.assertEqual(str(manifest_path), run["spec"]["scope"]["manifest"])

    def test_local_paths_are_copied_and_roles_assigned_by_group(self):
        local = self.root / "设计.docx"
        local.write_bytes(b"design bytes")
        response = self.post(body(attachments=[{"group_id": "design", "kind": "local_path", "path": str(local)}]))
        self.assertEqual(202, response.status_code, response.text)
        manifest_path = Path(response.json()["manifest_path"])
        entry = json.loads(manifest_path.read_text(encoding="utf-8"))["files"][0]
        self.assertEqual("requirement", entry["role"])
        local.write_bytes(b"changed")
        self.assertEqual(b"design bytes", (manifest_path.parent / entry["path"]).read_bytes())

    def test_owner_namespace_separates_bundles_and_run_queries(self):
        first = self.post().json()
        second = self.post(body(mineCode="mine-b")).json()
        self.assertNotEqual(Path(first["manifest_path"]).parent.parent, Path(second["manifest_path"]).parent.parent)
        response = self.client.get(f"/v1/runs/{first['items'][0]['run_id']}", params={
            "source_system": "debug-documents", "mine_code": "mine-b", "environment": "local"})
        self.assertEqual(404, response.status_code)
        copied = next(path for path in Path(first["manifest_path"]).parent.iterdir() if path.suffix == ".xlsx")
        response = self.post(body(mineCode="mine-b", attachments=[{
            "group_id": "design", "kind": "local_path", "path": str(copied)}]))
        self.assertEqual(422, response.status_code)
        self.assertIn("another owner", response.text)

    def test_invalid_metadata_and_formats_never_accept_a_run(self):
        invalid = (
            body(attachments=[upload(group_id="anything")]), body(items=[{"itemCode": "unknown"}]),
            body(items=[{"itemCode": "stop_work_unload"}, {"itemCode": "unknown"}]),
            body(period={"start": "2026-02-30", "end": "2026-10-31"}),
            body(period={"start": "2026-10-31", "end": "2026-10-01"}),
            body(source_complete="true"), body(attachments=[upload(name="models.local.toml")]),
            body(attachments=[upload(name="../daily.xlsx")]), body(attachments=[{"group_id": "prevention_daily", "kind": "file", "name": "日报.xlsx", "data": "invalid*"}]),
            body(attachments=[]), body(items=[{"itemCode": "stop_work_unload", "scope": {"fixture": "clean"}}]),
        )
        for request in invalid:
            with self.subTest(request=request):
                self.assert_rejected(request)

    def test_local_paths_cannot_disguise_config_or_escape_owner_storage(self):
        config = self.root / "config"
        config.mkdir()
        (config / "private.pdf").write_bytes(b"private")
        secret = self.root / "secret.toml"
        secret.write_bytes(b"private")
        other_namespace = digest({"source_system": "debug-documents", "mine_code": "mine-b", "environment": "local"})
        artifact = self.root / "artifacts" / other_namespace / "objects" / "private.pdf"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"other owner")
        for value in ("../design.docx", str(config / "private.pdf"), str(secret), str(artifact)):
            with self.subTest(value=value):
                self.assert_rejected(body(attachments=[{"group_id": "design", "kind": "local_path", "path": value, "name": "design.pdf"}]))

    def test_url_stream_download_and_duplicate_names(self):
        entries = [{"group_id": "dispatch_daily", "kind": "url", "url": f"https://example.test/{number}/日报.pdf?token=secret"}
                   for number in (1, 2)]
        with patch.object(debug_uploads, "build_opener") as opener:
            opener.return_value.open.side_effect = [DownloadResponse(b"one"), DownloadResponse(b"two")]
            response = self.post(body(attachments=entries))
        self.assertEqual(202, response.status_code, response.text)
        self.assertNotIn("secret", response.text)
        path = Path(response.json()["manifest_path"])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("secret", path.read_text(encoding="utf-8"))
        self.assertEqual([b"one", b"two"], [(path.parent / entry["path"]).read_bytes() for entry in manifest["files"]])

    def test_url_errors_redirects_and_credentials_leave_no_partial_bundle(self):
        for error in (HTTPError("https://secret.test?token=secret", 302, "redirect", {}, None),
                      HTTPError("https://secret.test?token=secret", 403, "secret", {}, None),
                      URLError("https://secret.test?token=secret")):
            with self.subTest(error=type(error).__name__), patch.object(debug_uploads, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                response = self.assert_rejected(body(attachments=[upload(), {"group_id": "design", "kind": "url", "url": "https://example.test/design.pdf"}]))
                self.assertNotIn("secret", response.text)
                self.assertEqual([], list((self.root / "uploads").glob("*/*/*")))
        for url in ("file:///private.pdf", "https://user:secret@example.test/design.pdf", "http://[invalid/design.pdf"):
            with self.subTest(url=url):
                self.assert_rejected(body(attachments=[{"group_id": "design", "kind": "url", "url": url}]))

    def test_redirect_handler_never_follows_a_new_target(self):
        self.assertIsNone(debug_uploads._RejectRedirects().redirect_request(None, None, 302, "", {}, "http://another.test/file.pdf"))

    def test_unicode_url_is_encoded_without_double_encoding_existing_escapes(self):
        url = "https://example.test/日报%20一.pdf?name=煤矿&token=abc%2F123"
        with patch.object(debug_uploads, "build_opener") as opener:
            opener.return_value.open.return_value = DownloadResponse(b"record")
            response = self.post(body(attachments=[{"group_id": "prevention_daily", "kind": "url", "url": url}]))
            self.assertEqual(202, response.status_code, response.text)
            request = opener.return_value.open.call_args.args[0]
            self.assertIn("%E6%97%A5%E6%8A%A5%20%E4%B8%80.pdf", request.full_url)
            self.assertIn("token=abc%2F123", request.full_url)

    def test_partial_acceptance_error_keeps_input_for_the_accepted_run(self):
        original = self.service.submit
        def accept_then_fail(request):
            original(request)
            raise RuntimeError("later item unavailable")
        with patch.object(self.service, "submit", side_effect=accept_then_fail):
            with self.assertRaisesRegex(RuntimeError, "later item unavailable"):
                debug_uploads.submit_bundle(self.service, body())
        run = self.service.ledger.claim("test")
        manifest_path = Path(run["spec"]["scope"]["manifest"])
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue((manifest_path.parent / manifest["files"][0]["path"]).is_file())

    def test_file_bundle_download_and_request_limits(self):
        with patch.object(debug_uploads, "MAX_FILE_BYTES", 3), patch.object(debug_uploads, "MAX_BUNDLE_BYTES", 5):
            self.assert_rejected(body(attachments=[upload(b"1234")]))
            self.assert_rejected(body(attachments=[upload(b"123"), upload(b"123")]))
            with patch.object(debug_uploads, "build_opener") as opener:
                opener.return_value.open.return_value = DownloadResponse(b"1234")
                self.assert_rejected(body(attachments=[{"group_id": "design", "kind": "url", "url": "https://example.test/design.pdf"}]))
        with patch.object(debug_uploads, "MAX_REQUEST_BYTES", 20):
            response = self.post()
            self.assertEqual(413, response.status_code)
            response = self.client.post("/v1/debug/bundles", content=iter([b"{" * 10, b"x" * 11]), headers={"content-type": "application/json"})
            self.assertEqual(413, response.status_code)
        self.assertIsNone(self.service.ledger.claim("test"))


if __name__ == "__main__":
    unittest.main()
