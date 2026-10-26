from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compliance_service.contracts import InvalidRequest, StaleLease
from compliance_service.fixtures import evaluate, load_snapshot
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


def request(scenario="clean", **overrides):
    return {"sourceSystem": "test-source", "mineCode": "mine-a", "checkCycle": " 2026-10 ",
            "scope": {"fixture": scenario}, "items": [{"itemCode": "stop_work_unload"}], **overrides}


class SimulatedCrash(BaseException):
    pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000.0]
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: self.clock[0])
        self.service = InspectionService(self.ledger)

    def get(self, run_id):
        return self.ledger.get(run_id, "test-source", "mine-a", "local")

    def test_five_terminal_results_new_rounds_and_immutable_history(self):
        for scenario, expected in (("clean", "pass"), ("violation", "no_pass"), ("missing", "pending"),
                                   ("unreadable", "unverifiable"), ("source_error", "failed")):
            with self.subTest(scenario=scenario):
                first = self.service.submit(request(scenario))["items"][0]["run_id"]
                self.assertEqual(expected, self.service.run_once()["status"])
                before = self.get(first)
                self.assertEqual(" 2026-10 ", before["result"]["checkCycle"])
                second = self.service.submit(request(scenario))["items"][0]["run_id"]
                self.assertNotEqual(first, second)
                self.service.run_once()
                self.assertEqual(before["result"], self.get(first)["result"])

    def test_multiple_items_failure_is_independent(self):
        body = request(items=[{"itemCode": "stop_work_unload", "scope": {"fixture": "source_error"}},
                              {"itemCode": "relief_effects", "scope": {"fixture": "clean"}}])
        accepted = self.service.submit(body)
        outcomes = {self.service.run_once()["status"] for _ in range(2)}
        self.assertEqual({"failed", "pass"}, outcomes)
        self.service.deliver_once()
        self.service.deliver_once()
        self.assertEqual(2, len(self.ledger.list_mock_receipts()))
        self.assertEqual({"delivered"}, {self.get(a["run_id"])["delivery_state"] for a in accepted["items"]})

    def test_crash_after_snapshot_resumes_without_reading_source_again(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        def crash(point):
            if point == "after_snapshot":
                raise SimulatedCrash()
        self.service.fault_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.run_once("dead-worker", lease_seconds=5)
        saved = self.get(run_id)["snapshot"]
        self.clock[0] += 6
        reopened = Ledger(self.path, clock=lambda: self.clock[0])
        def forbidden_source(run):
            raise AssertionError("saved snapshot must be reused")
        recovered = InspectionService(reopened, source=forbidden_source)
        self.assertEqual("pass", recovered.run_once("new-worker")["status"])
        self.assertEqual(saved, self.get(run_id)["snapshot"])
        self.assertIsNone(recovered.run_once())

    def test_crash_after_accept_claim_and_finish_does_not_lose_or_repeat(self):
        for crash_point in ("after_claim", "after_finish"):
            with self.subTest(point=crash_point):
                run_id = self.service.submit(request())["items"][0]["run_id"]
                def crash(point):
                    if point == crash_point:
                        raise SimulatedCrash()
                service = InspectionService(self.ledger, fault_hook=crash)
                with self.assertRaises(SimulatedCrash):
                    service.run_once(lease_seconds=5)
                self.clock[0] += 6
                self.service.run_once()
                self.assertEqual("completed", self.get(run_id)["execution_state"])
                self.assertIsNone(self.service.run_once())

    def test_lost_mock_ack_retries_same_delivery_without_recomputing(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        result = self.service.run_once()
        def crash(point):
            if point == "after_mock_receive":
                raise SimulatedCrash()
        self.service.fault_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.service.deliver_once(lease_seconds=5)
        self.assertEqual(1, len(self.ledger.list_mock_receipts()))
        self.clock[0] += 6
        self.service.fault_hook = lambda point: None
        self.assertEqual("delivered", self.service.deliver_once()["state"])
        self.assertEqual(1, len(self.ledger.list_mock_receipts()))
        self.assertEqual(result, self.get(run_id)["result"])
        self.assertIsNone(self.service.run_once())

    def test_callback_failure_only_retries_saved_payload(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        result = self.service.run_once()
        def timeout(*args):
            raise TimeoutError("synthetic acknowledgement lost")
        self.assertEqual("waiting", self.service.deliver_once(receiver=timeout)["state"])
        self.assertIsNone(self.service.run_once())
        self.clock[0] += 100
        self.assertEqual("delivered", self.service.deliver_once()["state"])
        self.assertEqual(result, self.get(run_id)["result"])

    def test_full_workflow_has_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("M1 must not open network sockets")):
            for scenario in ("clean", "violation", "missing", "unreadable", "source_error"):
                self.service.submit(request(scenario))
                self.service.run_once()
                self.service.deliver_once()

    def test_missing_or_unreadable_is_not_pass_and_violation_retains_gaps(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        run = self.get(run_id)
        snapshot = load_snapshot(run)
        snapshot["source_complete"] = False
        snapshot["coverage_gaps"] = ["unobserved source window"]
        self.assertEqual("pending", evaluate(run, snapshot)["status"])
        snapshot["propositions"][0]["state"] = "false"
        result = evaluate(run, snapshot)
        self.assertEqual("no_pass", result["status"])
        self.assertEqual(["unobserved source window"], result["coverage_gaps"])

    def test_source_change_does_not_change_active_request_identity(self):
        # Source discovery has not happened: acceptance is based solely on request scope.
        first = self.service.submit(request())["items"][0]
        second = self.service.submit(request())["items"][0]
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertIsNone(self.get(first["run_id"])["snapshot"])

    def test_unregistered_evidence_cannot_complete_and_other_run_snapshot_rejected(self):
        self.service.submit(request())
        a = self.ledger.claim("worker-a")
        self.service.submit(request(mineCode="mine-b"))
        b = self.ledger.claim("worker-b")
        snapshot = load_snapshot(a)
        with self.assertRaises(InvalidRequest):
            self.ledger.save_snapshot(b["run_id"], "worker-b", b["fence"], snapshot)
        self.ledger.save_snapshot(a["run_id"], "worker-a", a["fence"], snapshot)
        result = evaluate(a, snapshot)
        result["evidence_refs"] = ["not-registered"]
        with self.assertRaises(InvalidRequest):
            self.ledger.finish(a["run_id"], "worker-a", a["fence"], result)
        self.assertIsNone(self.get(a["run_id"])["result"])

    def test_no_callback_and_no_problem_attachment_omits_content(self):
        run_id = self.service.submit(request(callbackMode="none"))["items"][0]["run_id"]
        result = self.service.run_once()
        self.assertTrue(all("attachmentContent" not in a for a in result["attachments"]))
        self.assertIsNone(self.service.deliver_once())
        self.assertEqual("none", self.get(run_id)["delivery_state"])

    def test_missing_necessary_proposition_cannot_pass(self):
        run_id = self.service.submit(request(items=[{"itemCode": "relief_effects"}]))["items"][0]["run_id"]
        run = self.get(run_id)
        snapshot = load_snapshot(run)
        snapshot["propositions"] = snapshot["propositions"][:1]
        self.assertEqual("pending", evaluate(run, snapshot)["status"])

    def test_duplicate_unknown_conditions_and_false_completeness_cannot_pass(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        run = self.get(run_id)
        for variant in ("duplicate", "unknown", "unverified-completeness"):
            snapshot = load_snapshot(run)
            if variant == "unverified-completeness":
                snapshot["source_complete"] = "false"
                self.assertEqual("pending", evaluate(run, snapshot)["status"])
                continue
            if variant == "duplicate":
                snapshot["propositions"].append(snapshot["propositions"][0].copy())
            else:
                snapshot["propositions"][0]["code"] = "not-in-definition"
            with self.assertRaises(InvalidRequest):
                evaluate(run, snapshot)

    def test_repeated_crashes_stop_at_attempt_limit_and_leave_next_item_available(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        for number in range(3):
            self.assertEqual(run_id, self.ledger.claim(f"crashed-{number}", lease_seconds=1)["run_id"])
            self.clock[0] += 2
        self.service.submit(request(items=[{"itemCode": "relief_effects"}]))
        result = self.service.run_once()
        self.assertEqual("failed", result["status"])
        self.assertEqual(4, result["execution_attempt"])
        self.assertIn("attempt limit", result["detail"])
        self.assertEqual("pass", self.service.run_once()["status"])

    def test_unexpected_source_and_evaluator_errors_do_not_block_next_item(self):
        for failing_phase in ("source", "evaluator", "result_validation"):
            with self.subTest(phase=failing_phase):
                body = request(checkCycle=failing_phase, items=[{"itemCode": "stop_work_unload"}, {"itemCode": "relief_effects"}])
                accepted = self.service.submit(body)["items"]
                def source(run):
                    if failing_phase == "source" and run["spec"]["item_code"] == "stop_work_unload":
                        raise ValueError("synthetic source bug")
                    return load_snapshot(run)
                def evaluator(run, snapshot):
                    if run["spec"]["item_code"] == "stop_work_unload":
                        if failing_phase == "evaluator":
                            raise ValueError("synthetic evaluator bug")
                        if failing_phase == "result_validation":
                            bad = evaluate(run, snapshot)
                            bad["status"] = "invalid-code"
                            return bad
                    return evaluate(run, snapshot)
                service = InspectionService(self.ledger, source=source, evaluator=evaluator)
                results = [service.run_once(), service.run_once()]
                self.assertEqual({"failed", "pass"}, {r["status"] for r in results})
                failed = next(self.get(a["run_id"]) for a in accepted if self.get(a["run_id"])["result"]["status"] == "failed")
                self.assertTrue(failed["result"]["error_type"])
                if failing_phase != "source":
                    self.assertEqual(failed["snapshot"]["snapshot_id"], failed["result"]["snapshot_id"])

    def test_run_progress_reports_phases_and_preserves_failed_stage(self):
        run_id = self.service.submit(request())["items"][0]["run_id"]
        self.service.run_once()
        progress = self.ledger.get_progress(run_id, "test-source", "mine-a", "local")
        stages = [(event["stage"], event["event"]) for event in progress["events"]]
        self.assertIn(("source", "started"), stages)
        self.assertIn(("evaluation", "finished"), stages)
        self.assertEqual(("run", "finished"), stages[-1])
        timed = [event for event in progress["events"] if "elapsed_ms" in event]
        self.assertEqual(2, len(timed))
        run_id = self.service.submit(request("source_error"))["items"][0]["run_id"]
        self.service.run_once()
        events = self.ledger.get_progress(run_id, "test-source", "mine-a", "local")["events"]
        self.assertTrue(any(event["stage"] == "source" and event["event"] == "failed" for event in events))

    def test_progress_callback_uses_claimed_worker_and_fence(self):
        # Only the built-in local source receives this keyword; custom fixture
        # sources retain their existing one-argument callable contract.
        run = {"run_id": "r", "worker_id": "worker-a", "fence": 7,
               "spec": {"scope": {"manifest": "manifest.json"}}}
        captured = []
        class Source:
            def __init__(self, storage):
                pass
            def __call__(self, actual_run, *, progress_callback=None):
                self_event = {"stage": "recognition", "event": "provider_finished", "elapsed_ms": 12}
                progress_callback(self_event)
                return {"ok": True}
        with patch("compliance_service.sources.LocalDocumentSource", Source), \
             patch.object(self.ledger, "record_progress", side_effect=lambda *args: captured.append(args)):
            self.assertEqual({"ok": True}, self.service._source(run))
        self.assertEqual(("r", "worker-a", 7), captured[0][:3])
        with patch("compliance_service.sources.LocalDocumentSource", Source), \
             patch.object(self.ledger, "record_progress", side_effect=StaleLease("stale")):
            with self.assertRaises(StaleLease):
                self.service._source(run)


if __name__ == "__main__":
    unittest.main()
