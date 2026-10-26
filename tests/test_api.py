from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.contracts import DEFINITIONS
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


def request_body(**overrides):
    return {
        "sourceSystem": "source-a",
        "mineCode": "mine-a",
        "checkCycle": " 2026-10 ",
        "scope": {"fixture": "clean"},
        "items": [{"itemCode": "stop_work_unload"}],
        **overrides,
    }


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = InspectionService(Ledger(Path(self.temp.name) / "ledger.sqlite3"))
        self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)

    def submit(self, **overrides):
        response = self.client.post("/v1/manual/runs", json=request_body(**overrides))
        self.assertEqual(202, response.status_code, response.text)
        return response.json()["items"][0]["run_id"]

    def get(self, run_id, **overrides):
        return self.client.get(
            f"/v1/runs/{run_id}",
            params={"source_system": "source-a", "mine_code": "mine-a", "environment": "local", **overrides},
        )

    def test_both_entries_share_active_run_and_preserve_cycle(self):
        run_id = self.submit()
        upstream = self.client.post("/v1/upstream/runs", json=request_body())
        self.assertEqual(202, upstream.status_code)
        self.assertEqual(run_id, upstream.json()["items"][0]["run_id"])
        self.assertFalse(upstream.json()["items"][0]["created"])
        self.assertEqual(" 2026-10 ", self.get(run_id).json()["spec"]["check_cycle"])
        self.service.run_once()
        self.assertEqual(" 2026-10 ", self.get(run_id).json()["result"]["checkCycle"])

    def test_get_requires_all_owner_conditions_and_hides_other_owners(self):
        run_id = self.submit()
        self.assertEqual(200, self.get(run_id).status_code)
        for query_name, other in (("source_system", "other-source"), ("mine_code", "other-mine"), ("environment", "test")):
            with self.subTest(query_name=query_name):
                self.assertEqual(404, self.get(run_id, **{query_name: other}).status_code)
        for missing in ("source_system", "mine_code", "environment"):
            params = {"source_system": "source-a", "mine_code": "mine-a", "environment": "local"}
            del params[missing]
            self.assertEqual(422, self.client.get(f"/v1/runs/{run_id}", params=params).status_code)
        self.assertEqual(404, self.get("unknown-run").status_code)

    def test_invalid_requests_do_not_accept_any_batch_items(self):
        for invalid in (
            [], "not-an-object", 7,
            request_body(callbackMode="real"),
            request_body(items=[{"itemCode": "unknown"}]),
            request_body(items=[{"itemCode": "stop_work_unload"}, {"itemCode": "unknown"}]),
        ):
            with self.subTest(invalid=invalid):
                response = self.client.post("/v1/manual/runs", json=invalid)
                self.assertEqual(422, response.status_code, response.text)
                self.assertIsNone(self.service.run_once())

    def test_active_scope_callback_mode_conflict_is_409(self):
        self.submit()
        response = self.client.post("/v1/upstream/runs", json=request_body(callbackMode="none"))
        self.assertEqual(409, response.status_code)

    def test_definition_registration_is_labeled_as_engineering_mock(self):
        response = self.client.get("/v1/definitions")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("local-fixture", data["profile"])
        self.assertEqual(set(DEFINITIONS), {item["itemCode"] for item in data["items"]})
        self.assertTrue(all(item["engineering_mock"] for item in data["items"]))

    def test_health_only_claims_local_fixture_health(self):
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "profile": "local-fixture", "real_connectivity": False}, response.json())

    def test_debug_page_is_served_without_adding_a_mutation_route(self):
        for path in ("/debug/legacy",):
            response = self.client.get(path)
            self.assertEqual(200, response.status_code)
            self.assertIn("文档合规检查", response.text)
            self.assertIn("/v1/manual/runs", response.text)
            self.assertIn("模拟上层服务触发任务", response.text)
            self.assertIn("本次检查涉及文件", response.text)

    def test_local_debug_upstream_task_accepts_manual_parameters_and_disables_callback(self):
        root = Path(self.temp.name) / "docs"
        root.mkdir()
        source = root / "daily.txt"
        source.write_text("2026-10-14 403105工作面已完成停工。", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "source_system": "debug-local", "mine_code": "M-001", "environment": "local",
            "period": {"start": "2026-10-01", "end": "2026-10-31"}, "source_complete": False,
            "root": str(root), "files": [{"file_id": "daily", "path": "daily.txt", "role": "actual_record"}]
        }), encoding="utf-8")
        response = self.client.post("/v1/debug/upstream/tasks", json={
            "mineId": "M-001", "status": "running", "checkCycle": "2026-10",
            "items": [{"itemCode": "stop_work_unload"}], "manifestPath": str(manifest),
            "resultCallbackUrl": "https://debug.invalid/result", "sourceSystem": "debug-local", "environment": "local",
        })
        self.assertEqual(202, response.status_code, response.text)
        data = response.json()
        self.assertEqual("none", data["debug_task"]["callbackMode"])
        self.assertEqual("https://debug.invalid/result", data["debug_task"]["resultCallbackUrl"])
        run_id = data["items"][0]["run_id"]
        stored = self.service.ledger.get(run_id, "debug-local", "M-001", "local")
        self.assertEqual("none", stored["spec"]["callback_mode"])
        self.service.run_once()
        stored = self.service.ledger.get(run_id, "debug-local", "M-001", "local")
        self.assertEqual("none", stored["delivery_state"])

    def test_local_debug_upstream_task_rejects_real_callback_and_missing_manifest(self):
        body = {"mineId": "M-001", "status": "running", "checkCycle": "2026-10",
                "items": [{"itemCode": "stop_work_unload"}], "manifestPath": "C:/missing.json"}
        self.assertEqual(422, self.client.post("/v1/debug/upstream/tasks", json=body).status_code)
        body["manifestPath"] = str(Path(self.temp.name) / "missing.json")
        body["callbackMode"] = "real"
        self.assertEqual(422, self.client.post("/v1/debug/upstream/tasks", json=body).status_code)

    def test_historical_result_has_no_manual_mutation_route(self):
        run_id = self.submit(scope={"fixture": "missing"})
        self.service.run_once()
        before = self.get(run_id).json()
        self.assertEqual("pending", before["result"]["status"])
        for path in (f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/result", f"/v1/runs/{run_id}/review"):
            for method in ("post", "patch", "put"):
                response = getattr(self.client, method)(path, json={"status": "no_pass"})
                self.assertIn(response.status_code, (404, 405))
        self.assertEqual(before, self.get(run_id).json())

    def test_progress_endpoint_is_paginated_and_owner_scoped(self):
        run_id = self.submit()
        self.service.run_once()
        params = {"source_system": "source-a", "mine_code": "mine-a", "environment": "local"}
        response = self.client.get(f"/v1/runs/{run_id}/progress", params={**params, "limit": 2})
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()
        self.assertEqual(2, len(first["events"]))
        self.assertEqual("completed", first["execution_state"])
        self.assertTrue(first["has_more"])
        second = self.client.get(f"/v1/runs/{run_id}/progress",
                                 params={**params, "after": first["next_after"]}).json()
        self.assertGreater(second["events"][0]["sequence"], first["next_after"])
        self.assertEqual("finished", second["events"][-1]["event"])
        self.assertIn("progress_summary", self.get(run_id).json())
        for key in params:
            self.assertEqual(404, self.client.get(f"/v1/runs/{run_id}/progress",
                                                  params={**params, key: "other"}).status_code)
            incomplete = {k: value for k, value in params.items() if k != key}
            self.assertEqual(422, self.client.get(f"/v1/runs/{run_id}/progress", params=incomplete).status_code)
        for invalid in ({"after": -1}, {"limit": 0}, {"limit": 501}, {"after": "bad"}):
            self.assertEqual(422, self.client.get(f"/v1/runs/{run_id}/progress",
                                                  params={**params, **invalid}).status_code)


if __name__ == "__main__":
    unittest.main()
