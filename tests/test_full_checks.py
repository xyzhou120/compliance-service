import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compliance_service.evaluation import run_evaluation
from compliance_service.samples import create_validation_samples
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class FullChecksTests(unittest.TestCase):
    def test_both_complete_checks_and_missing_or_bad_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            created = create_validation_samples(root / "samples")
            report = run_evaluation(created["case_file"], root / "evaluation")
            self.assertEqual(8, report["case_count"])
            self.assertEqual(1.0, report["label_accuracy"], report)
            self.assertEqual(0, report["unsafe_pass_count"])
            self.assertEqual(0, report["untraceable_case_count"])

    def test_manual_api_accepts_both_full_checks_and_delivers_results(self):
        from fastapi.testclient import TestClient
        from compliance_service.api import create_app
        with TemporaryDirectory() as directory:
            root = Path(directory)
            created = create_validation_samples(root / "samples")
            service = InspectionService(Ledger(root / "test.sqlite3"))
            with TestClient(create_app(service)) as client:
                response = client.post("/v1/manual/runs", json={
                    "sourceSystem": "acceptance-samples", "mineCode": "SAMPLE-MINE", "environment": "test",
                    "checkCycle": "2026-10", "scope": {"manifest": created["complete_manifest"]}, "callbackMode": "mock",
                    "items": [{"itemCode": "stop_work_unload"}, {"itemCode": "relief_effects"}],
                })
                self.assertEqual(202, response.status_code, response.text)
                service.run_once()
                service.run_once()
                service.deliver_once()
                service.deliver_once()
                for item in response.json()["items"]:
                    checked = client.get("/v1/runs/" + item["run_id"], params={
                        "source_system": "acceptance-samples", "mine_code": "SAMPLE-MINE", "environment": "test",
                    }).json()
                    self.assertEqual("completed", checked["execution_state"])
                    self.assertEqual("pass", checked["result"]["status"])
                    self.assertTrue(checked["result"]["rule_coverage"]["full_rule_check_complete"])
                    self.assertEqual("delivered", checked["delivery_state"])
