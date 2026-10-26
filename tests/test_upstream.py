import unittest

from compliance_service.contracts import InvalidRequest
from compliance_service.upstream import (
    control_response,
    normalize_upstream_task,
    validate_control_status,
)


def payload(**overrides):
    body = {
        "mineId": "610401015959",
        "status": "running",
        "checkCycle": "2026-10",
        "items": [{"itemCode": "stop_work_unload", "itemName": "冲击危险停工撤人与卸压"}],
        "resultCallbackUrl": "https://api.example.com/fileAudit/mineDataInspectionTask/resultCallback",
    }
    body.update(overrides)
    return body


class UpstreamContractTests(unittest.TestCase):
    def test_normalizes_sample_and_preserves_mine_id_and_raw_payload(self):
        body = payload(taskId="task-001")
        result = normalize_upstream_task(body)
        self.assertEqual("610401015959", result["mine_id"])
        self.assertEqual("task:task-001", result["task_key"])
        self.assertEqual(body, result["raw_payload"])
        body["mineId"] = "changed"
        self.assertEqual("610401015959", result["raw_payload"]["mineId"])

    def test_repeated_control_statuses_have_same_key_without_explicit_id(self):
        first = normalize_upstream_task(payload())
        second = normalize_upstream_task(payload(status="paused"), status_map={"paused": "pause"})
        self.assertEqual(first["task_key"], second["task_key"])
        self.assertEqual("pause", second["action"])
        with self.assertRaises(InvalidRequest):
            normalize_upstream_task(payload(status="paused"))

    def test_execution_id_is_stable_identity(self):
        result = normalize_upstream_task(payload(executionId="exec-7"))
        self.assertTrue(result["task_key"].startswith("execution:"))
        self.assertEqual(result["task_key"], normalize_upstream_task(payload(executionId="exec-7"))["task_key"])
        self.assertNotEqual(result["task_key"], normalize_upstream_task(payload(executionId="exec-8"))["task_key"])

    def test_rejects_invalid_items_and_urls(self):
        cases = (
            payload(items=[{"itemCode": "unknown"}]),
            payload(items=[{"itemCode": "stop_work_unload", "itemName": "wrong"}]),
            payload(resultCallbackUrl="http://api.example.com/callback"),
            payload(resultCallbackUrl="https://user:pass@api.example.com/callback"),
            payload(resultCallbackUrl="https://api.example.com/callback#fragment"),
            payload(items=[{"itemCode": "stop_work_unload"}, {"itemCode": "stop_work_unload"}]),
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(InvalidRequest):
                    normalize_upstream_task(body)

    def test_rejects_unknown_fields_and_status(self):
        with self.assertRaises(InvalidRequest):
            normalize_upstream_task(payload(unrelated="value"))
        with self.assertRaises(InvalidRequest):
            normalize_upstream_task(payload(status="mystery"))

    def test_control_validation_and_response(self):
        self.assertEqual("pause", validate_control_status("pause"))
        with self.assertRaises(InvalidRequest):
            validate_control_status("completed")
        self.assertEqual({"code": 200, "success": True, "msg": "任务控制请求已生效"}, control_response())

    def test_callback_change_is_same_identity_to_allow_ledger_conflict_check(self):
        first = normalize_upstream_task(payload())
        second = normalize_upstream_task(payload(resultCallbackUrl="https://other.example.com/callback"))
        self.assertEqual(first["task_key"], second["task_key"])
        self.assertNotEqual(first["callback_url"], second["callback_url"])

    def test_item_order_and_optional_names_do_not_create_new_identity(self):
        first = normalize_upstream_task(payload(items=[
            {"itemCode": "stop_work_unload", "itemName": "冲击危险停工撤人与卸压"},
            {"itemCode": "relief_effects", "itemName": "冲击地压解危效果检验"},
        ]))
        second = normalize_upstream_task(payload(items=[
            {"itemCode": "relief_effects"}, {"itemCode": "stop_work_unload"},
        ]))
        self.assertEqual(first["task_key"], second["task_key"])
        self.assertEqual("stop_work_unload", first["items"][0]["item_code"])

    def test_mine_identifier_keeps_leading_zeros_and_requires_string(self):
        result = normalize_upstream_task(payload(mineId="001234"))
        self.assertEqual("001234", result["mine_code"])
        with self.assertRaises(InvalidRequest):
            normalize_upstream_task(payload(mineId=1234))

    def test_invalid_status_mapping_and_malformed_urls_are_rejected(self):
        with self.assertRaises(InvalidRequest):
            normalize_upstream_task(payload(status="paused"), status_map={"paused": "invented-action"})
        for url in ("https://[broken", "https://api.example.com:bad/callback", "https://api.example.com/call\nback"):
            with self.subTest(url=url), self.assertRaises(InvalidRequest):
                normalize_upstream_task(payload(resultCallbackUrl=url))


if __name__ == "__main__":
    unittest.main()
