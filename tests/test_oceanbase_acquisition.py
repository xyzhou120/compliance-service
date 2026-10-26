import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from compliance_service.acquisition import AttachmentAcquisition, ReceiptWriteError, _replace_with_retry
from compliance_service.contracts import InvalidRequest
from compliance_service.oceanbase import OceanBaseRepository, _period
from compliance_service.inspection_config import default_config, Registry
from datetime import date

class Cursor:
    description = [("attachment_id",), ("file_name_id",), ("attachment_name",), ("attachment_url",), ("report_date",)]
    def __init__(self, rows): self.rows = rows; self.sql = None; self.params = None
    def execute(self, sql, params): self.sql, self.params = sql, params
    def fetchall(self): return self.rows
    def close(self): pass
class Conn:
    def __init__(self, rows): self.cursor_obj = Cursor(rows)
    def cursor(self): return self.cursor_obj
    def close(self): pass

class OceanBaseAcquisitionTests(unittest.TestCase):
    def _snapshot(self):
        return {"profile": "oceanbase-catalog", "snapshot_id": "receipt-snapshot", "mine_code": "m",
                "period": {"start": "2026-10-01", "end": "2026-10-31"}, "query": {"query_version": "v"},
                "rows": [{"attachment_id": "a1", "attachment_name": "日报.pdf",
                          "attachment_url": "https://files.test/a1.pdf", "role": "actual_record",
                          "material_group": "prevention_daily", "report_date": "2026-10-14",
                          "file_name_id": "f"}]}

    def test_receipt_replace_retries_short_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "receipt.part", Path(td) / "receipt.json"
            source.write_text("{}", encoding="utf-8")
            real_replace = __import__("os").replace
            calls = {"count": 0}
            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise PermissionError("sharing violation")
                return real_replace(src, dst)
            with patch("compliance_service.acquisition.os.replace", side_effect=flaky_replace), \
                 patch("compliance_service.acquisition.sleep"):
                _replace_with_retry(source, target, attempts=4)
            self.assertEqual("{}", target.read_text(encoding="utf-8"))
            self.assertEqual(3, calls["count"])

    def test_receipt_replace_reports_final_windows_lock(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / "receipt.part", Path(td) / "receipt.json"
            source.write_text("{}", encoding="utf-8")
            with patch("compliance_service.acquisition.os.replace", side_effect=PermissionError("sharing violation")), \
                 patch("compliance_service.acquisition.sleep"):
                with self.assertRaises(ReceiptWriteError) as error:
                    _replace_with_retry(source, target, attempts=3)
            self.assertIn("3 attempts", str(error.exception))

    def test_downloaded_bytes_are_recovered_when_prior_manifest_records_receipt_failure(self):
        snapshot = self._snapshot()
        acquisition = AttachmentAcquisition(fetcher=lambda _: io.BytesIO(b"%PDF-1.7 body"), allowed_hosts={"files.test"})
        with tempfile.TemporaryDirectory() as td:
            # First run keeps the bytes but simulates a receipt publish failure.
            def receipt_lock(src, dst):
                if dst.name == "acquisition-receipts.json":
                    raise ReceiptWriteError("locked")
                return __import__("os").replace(src, dst)
            with patch("compliance_service.acquisition._replace_with_retry", side_effect=receipt_lock):
                output = acquisition.prepare(snapshot, td)
            first = json.loads(Path(output).read_text(encoding="utf-8"))
            self.assertEqual(2, len(first["acquisition"]["receipt_failures"]))
            self.assertEqual([], first["acquisition"]["failures"])
            self.assertEqual(1, len(first["files"]))
            with self.assertRaises(InvalidRequest):
                acquisition.prepare({**snapshot, "snapshot_id": "different-snapshot"}, td)
            # A later attempt reuses the frozen bytes by attachment ID and
            # publishes a durable receipt without downloading again.
            with patch.object(acquisition, "_download", side_effect=AssertionError("must reuse downloaded bytes")):
                output = acquisition.prepare(snapshot, td)
            second = json.loads(Path(output).read_text(encoding="utf-8"))
            self.assertNotIn("receipt_failures", second["acquisition"])
            with patch.object(acquisition, "_download", side_effect=AssertionError("must reuse downloaded bytes")):
                self.assertEqual(output, acquisition.prepare(snapshot, td))

    def test_failed_attachments_retry_without_downloading_successful_ids_again(self):
        snapshot = self._snapshot()
        snapshot["rows"].append({**snapshot["rows"][0], "attachment_id": "a2", "attachment_url": "https://files.test/a2.pdf"})
        requests = []
        def failing_fetch(url):
            requests.append(url)
            if url.endswith("a2.pdf"):
                raise TimeoutError("download temporarily unavailable")
            return io.BytesIO(b"%PDF-1.7 body")
        acquisition = AttachmentAcquisition(fetcher=failing_fetch, allowed_hosts={"files.test"})
        with tempfile.TemporaryDirectory() as td:
            output = acquisition.prepare(snapshot, td)
            first = json.loads(Path(output).read_text(encoding="utf-8"))
            self.assertEqual("download", first["acquisition"]["failures"][0]["stage"])
            acquisition.fetcher = lambda url: (requests.append(url) or io.BytesIO(b"%PDF-1.7 body"))
            output = acquisition.prepare(snapshot, td)
            self.assertEqual(1, requests.count("https://files.test/a1.pdf"))
            self.assertEqual(2, requests.count("https://files.test/a2.pdf"))
            self.assertEqual(2, len(json.loads(Path(output).read_text(encoding="utf-8"))["files"]))
    def test_configured_minio_url_encodes_literal_filename_characters(self):
        row = {"attachment_id": "a", "file_name_id": "rock_ledger_1k8hqc4_rpt_wh5nwe",
               "local_bucket_name": "disaster-attach", "local_attachment_address": "2026/a/日报 #1%2F.pdf"}
        repo = OceanBaseRepository(lambda: Conn([row]), attachment_base_url="https://files.test:8082/")
        snapshot = repo.query("001", "2026-10", "stop_work_unload")
        self.assertEqual("https://files.test:8082/disaster-attach/2026/a/%E6%97%A5%E6%8A%A5%20%231%252F.pdf", snapshot["rows"][0]["attachment_url"])
        for key in ("../secret.pdf", "a/../secret.pdf", "a\\secret.pdf"):
            row["local_attachment_address"] = key
            with self.subTest(key=key), self.assertRaises(InvalidRequest):
                repo.query("001", "2026-10", "stop_work_unload")

    def test_real_schema_address_urls_and_object_keys_are_distinct(self):
        row = {"attachment_id": "a", "file_name_id": "rock_ledger_1k8hqc4_rpt_wh5nwe",
               "attachment_address": "20261025/file.pdf", "bucket_name": "rock-burst",
               "local_attachment_address": "https://files.test/local.pdf"}
        snapshot = OceanBaseRepository(lambda: Conn([row])).query("001", "2026-10", "stop_work_unload")
        self.assertEqual("https://files.test/local.pdf", snapshot["rows"][0]["attachment_url"])
        row["local_attachment_address"] = "2026/a/file.pdf"
        snapshot = OceanBaseRepository(lambda: Conn([row])).query("001", "2026-10", "stop_work_unload")
        self.assertIsNone(snapshot["rows"][0]["attachment_url"])
        self.assertEqual("2026/a/file.pdf", snapshot["raw_rows"][0]["local_attachment_address"])

    def test_duplicate_object_keys_cannot_silently_rebind_attachment(self):
        first = {"attachment_id": "a", "file_name_id": "rock_ledger_1k8hqc4_rpt_wh5nwe",
                 "attachment_address": "first.pdf", "bucket_name": "rock-burst"}
        second = {**first, "attachment_address": "second.pdf"}
        with self.assertRaises(InvalidRequest):
            OceanBaseRepository(lambda: Conn([first, second])).query("001", "2026-10", "stop_work_unload")

    def test_configured_material_cadences_generate_year_and_month_predicates(self):
        config = default_config()
        item = config["items"][0]
        item["itemCode"] = "configured-stop-code"
        item["materials"] = [item["materials"][0], item["materials"][5], item["materials"][8]]
        item["materials"][0]["file_name_id"] = "daily-id-in-another-environment"
        item["materials"][2]["cadence"] = "semiannual"
        config["query_policy"] = {"month_source": "fixed", "fixed_month": "06"}
        connection = Conn([])
        snapshot = OceanBaseRepository(lambda: connection, inspection_config=config).query("001", "2026-10", "configured-stop-code")
        sql, params = connection.cursor_obj.sql, connection.cursor_obj.params
        self.assertIn("report_time_month = %s", sql)
        self.assertIn("report_time_month IN (%s, %s)", sql)
        self.assertNotIn("report_date >=", sql)
        self.assertNotIn("daily-id-in-another-environment", sql)
        self.assertIn("daily-id-in-another-environment", params)
        self.assertIn("10", params)
        self.assertNotIn("06", params)
        self.assertIn("01", params)
        self.assertIn("07", params)
        self.assertEqual("2026-10", snapshot["check_cycle"])
        self.assertEqual({"start": "2026-10-01", "end": "2026-10-31"}, snapshot["period"])
        self.assertTrue(snapshot["catalog"]["inspection_config_revision"])

    def test_explicit_cycle_wins_over_runtime_flags_and_only_frozen_scope_can_override(self):
        class February(date):
            @classmethod
            def today(cls): return cls(2026, 2, 10)
        repo = OceanBaseRepository(lambda: Conn([]))
        with patch("compliance_service.oceanbase.date", February):
            current = repo.query("001", "2026-10", "stop_work_unload")
            task = repo.query("001", "2026-10", "stop_work_unload", month_source="task_month")
            with self.assertRaises(InvalidRequest):
                repo.query("001", "2026-10", "stop_work_unload", resolved_month="08")
        self.assertEqual("10", current["query"]["query_policy"]["resolved_month"])
        self.assertEqual("10", task["query"]["query_policy"]["resolved_month"])

    def test_annual_only_materials_never_get_a_month_filter(self):
        config = default_config()
        config["items"][0]["materials"] = [m for m in config["items"][0]["materials"] if m["cadence"] == "annual"]
        conn = Conn([])
        OceanBaseRepository(lambda: conn, inspection_config=config).query("001", "2026-10", "stop_work_unload")
        self.assertNotIn("report_time_month", conn.cursor_obj.sql.split("ORDER BY")[0])
        self.assertIn(2026, conn.cursor_obj.params)

    def test_per_run_item_snapshot_is_used_after_registry_changes(self):
        config = default_config()
        item = config["items"][0]
        pinned = {**item, "query_policy": {"month_source": "current_month"},
                  "resolved_query_month": "04", "revision": "accepted-revision"}
        conn = Conn([])
        snapshot = OceanBaseRepository(lambda: conn).query("001", "2026-10", "stop_work_unload", inspection_config=pinned)
        self.assertEqual("04", snapshot["query"]["query_policy"]["resolved_month"])
        self.assertEqual("accepted-revision", snapshot["catalog"]["inspection_config_revision"])

    def test_query_uses_config_path_and_rejects_out_of_scope_period_rows(self):
        config = default_config()
        config["query_policy"] = {"month_source": "task_month"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/"catalog.json"
            registry = Registry(path)
            registry.save(config, registry.read()["revision"])
            repo = OceanBaseRepository(lambda: Conn([]), config_path=str(path))
            snapshot = repo.query("001", "2026-10", "relief_effects")
            self.assertEqual("10", snapshot["query"]["query_policy"]["resolved_month"])
        row = {"attachment_id":"x","file_name_id":"rock_ledger_1k8hqc4_rpt_wh5nwe", "report_time_year":2025, "report_time_month":"10"}
        with self.assertRaises(InvalidRequest):
            OceanBaseRepository(lambda: Conn([row])).query("001", "2026-10", "stop_work_unload")
    def test_invalid_calendar_year_is_a_request_error(self):
        for cycle in ("0000-01", "9999-12"):
            with self.subTest(cycle=cycle), self.assertRaises(InvalidRequest):
                _period(cycle)

    def rows(self):
        return [("a1", "rock_ledger_1k8hqc4_rpt_wh5nwe", "daily.pdf", "https://files.test/a1.pdf", "2026-10-14"),
                ("a1", "rock_ledger_1k8hqc4_rpt_wh5nwe", "daily.pdf", "https://files.test/a1.pdf", "2026-10-14"),
                ("a2", "rock_ledger_1k8hqc4_rpt_wh5nwe", "daily.pdf", "https://files.test/a2.pdf", "2026-10-14"),
                ("a3", "rock_x_iverp1_x_1t8xner", "design.docx", "https://files.test/a3.docx", "2026") ]
    def test_query_is_parameterized_and_deduplicates_by_attachment_id(self):
        conn = Conn(self.rows())
        repo = OceanBaseRepository(lambda: conn)
        snapshot = repo.query("610401015959", "2026-10", "stop_work_unload")
        self.assertEqual(["a1", "a2", "a3"], [r["attachment_id"] for r in snapshot["rows"]])
        self.assertEqual("2026-10-01", snapshot["period"]["start"])
        self.assertEqual("2026-10-31", snapshot["period"]["end"])
        self.assertIn("file_name_id IN", conn.cursor_obj.sql)
        self.assertNotIn("610401015959", conn.cursor_obj.sql)
        self.assertEqual("610401015959", conn.cursor_obj.params[0])
        self.assertIn(2026, conn.cursor_obj.params)
        self.assertEqual(4, len(snapshot["raw_rows"]))
        self.assertFalse(snapshot["source_complete"])
        self.assertEqual(1, len(snapshot["conflicts"]))

    def test_query_requires_explicit_mysql_mode_and_cycle(self):
        with self.assertRaises(InvalidRequest):
            OceanBaseRepository(lambda: Conn([]), compatibility_mode="oracle")
        repo = OceanBaseRepository(lambda: Conn([]))
        with self.assertRaises(InvalidRequest): repo.query("m", "2026/10", "stop_work_unload")

    def test_prepare_streams_by_id_and_emits_local_manifest(self):
        snapshot = {"profile": "oceanbase-catalog", "snapshot_id": "snap", "mine_code": "m",
                    "period": {"start": "2026-10-01", "end": "2026-10-31"},
                    "query": {"query_version": "v"},
                    "rows": [{"attachment_id": "a1", "attachment_name": "日报.pdf", "attachment_url": "https://files.test/a1.pdf", "role": "actual_record", "material_group": "prevention_daily", "report_date": "2026-10-14", "file_name_id": "f"}]}
        def fetcher(url):
            self.assertEqual("https://files.test/a1.pdf", url); return io.BytesIO(b"%PDF-1.7\nbody")
        with tempfile.TemporaryDirectory() as td:
            path = AttachmentAcquisition(fetcher=fetcher, allowed_hosts={"files.test"}).prepare(snapshot, td)
            manifest = json.loads(Path(path).read_text())
            self.assertTrue(Path(path).is_absolute())
            self.assertFalse(manifest["source_complete"])
            self.assertEqual("a1", manifest["files"][0]["file_id"])
            self.assertTrue(any(x.suffix == ".pdf" and x.name != "manifest.json" for x in Path(td).iterdir()))

    def test_prepare_rejects_http_and_magic_mismatch_and_cleans_part(self):
        snapshot = {"profile": "oceanbase-catalog", "snapshot_id": "s", "mine_code": "m", "period": {"start": "2026-10-01", "end": "2026-10-31"}, "rows": [{"attachment_id": "a", "attachment_name": "x.pdf", "attachment_url": "http://files.test/x.pdf"}]}
        with tempfile.TemporaryDirectory() as td:
            path = AttachmentAcquisition(fetcher=lambda _: io.BytesIO(b"bad"), allowed_hosts={"files.test"}).prepare(snapshot, td)
            manifest = json.loads(Path(path).read_text())
            self.assertEqual([], manifest["files"]); self.assertEqual("failed", manifest["acquisition"]["failures"][0]["status"])
            self.assertFalse(any(x.name.endswith(".pdf.part") for x in Path(td).iterdir()))

    def test_cross_mine_and_conflicting_duplicate_metadata_are_rejected(self):
        row = {"attachment_id": "a", "file_name_id": "rock_ledger_1k8hqc4_rpt_wh5nwe", "mine_code": "other"}
        with self.assertRaises(InvalidRequest):
            OceanBaseRepository(lambda: Conn([row])).query("m", "2026-10", "stop_work_unload")
        first = {"attachment_id": "a", "file_name_id": "rock_ledger_1k8hqc4_rpt_wh5nwe", "attachment_url": "https://files.test/a"}
        second = {**first, "attachment_url": "https://files.test/b"}
        with self.assertRaises(InvalidRequest):
            OceanBaseRepository(lambda: Conn([first, second])).query("m", "2026-10", "stop_work_unload")

    def test_frozen_receipt_blocks_rebinding_and_detects_tampering(self):
        snapshot = {"profile": "oceanbase-catalog", "snapshot_id": "s", "mine_code": "m", "period": {"start": "2026-10-01", "end": "2026-10-31"}, "rows": [{"attachment_id": "a", "attachment_name": "x.pdf", "attachment_url": "https://files.test/x.pdf"}]}
        acquisition = AttachmentAcquisition(fetcher=lambda _: io.BytesIO(b"%PDF-1.7 body"), allowed_hosts={"files.test"})
        with tempfile.TemporaryDirectory() as td:
            output = acquisition.prepare(snapshot, td)
            self.assertTrue(Path(output).exists())
            changed = {**snapshot, "snapshot_id": "another"}
            with self.assertRaises(InvalidRequest): acquisition.prepare(changed, td)
            next(x for x in Path(td).iterdir() if x.suffix == ".pdf" and x.name != "manifest.json").write_bytes(b"%PDF-1.7 changed")
            with self.assertRaises(InvalidRequest): acquisition.prepare(snapshot, td)

    def test_pause_checkpoint_is_not_swallowed_and_allowlist_is_required(self):
        with self.assertRaises(ValueError): AttachmentAcquisition()
        snapshot = {"profile": "oceanbase-catalog", "snapshot_id": "s", "mine_code": "m", "period": {"start": "2026-10-01", "end": "2026-10-31"}, "rows": [{"attachment_id": "a", "attachment_name": "x.pdf", "attachment_url": "https://files.test/x.pdf"}]}
        def paused(event):
            if event["event"] == "attachment_finished": raise RuntimeError("pause")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "pause"):
                AttachmentAcquisition(fetcher=lambda _: io.BytesIO(b"%PDF-1.7 body"), allowed_hosts={"files.test"}).prepare(snapshot, td, progress_callback=paused)

    def test_real_opener_resolves_and_rejects_metadata_address(self):
        class Opener:
            def open(self, *args, **kwargs): raise AssertionError("opener must not receive denied address")
        acquisition = AttachmentAcquisition(opener=Opener(), allowed_hosts={"files.test"})
        with tempfile.TemporaryDirectory() as td:
            with patch("compliance_service.acquisition.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 443))]):
                with self.assertRaises(Exception):
                    acquisition._download("https://files.test/x.pdf", Path(td) / "x.pdf")

    def test_case_and_reserved_attachment_ids_have_independent_digest_paths(self):
        snapshot = {"profile": "oceanbase-catalog", "snapshot_id": "s", "mine_code": "m", "period": {"start": "2026-10-01", "end": "2026-10-31"}, "rows": [{"attachment_id": aid, "attachment_name": "x.pdf", "attachment_url": "https://files.test/" + aid + ".pdf"} for aid in ("a", "A", "CON")]}
        events = []
        with tempfile.TemporaryDirectory() as td:
            output = AttachmentAcquisition(fetcher=lambda _: io.BytesIO(b"%PDF-1.7 body"), allowed_hosts={"files.test"}).prepare(snapshot, td, progress_callback=events.append)
            manifest = json.loads(Path(output).read_text())
            self.assertEqual(3, len({entry["path"].casefold() for entry in manifest["files"]}))
            self.assertFalse(any(Path(entry["path"]).stem.upper() == "CON" for entry in manifest["files"]))
            completed = [event for event in events if event["event"] == "attachment_finished"]
            self.assertTrue(all(event.get("file_id") and event["elapsed_ms"] >= 0 for event in completed))

if __name__ == "__main__": unittest.main()
