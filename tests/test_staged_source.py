import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from compliance_service.contracts import RunSpec
from compliance_service.sources import LocalDocumentSource


class StagedSourceTests(unittest.TestCase):
    def test_oceanbase_stop_item_scans_trigger_inputs_first_and_keeps_all_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for file_id, group in (("design", "design"), ("dispatch", "dispatch_daily"),
                                   ("threshold", "threshold"), ("daily", "prevention_daily")):
                (root / (file_id + ".docx")).write_bytes(file_id.encode())
                entries.append({"file_id": file_id, "path": file_id + ".docx", "role": "requirement" if group in {"design", "threshold"} else "actual_record",
                                "material_group": group})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"source_system": "test", "mine_code": "001", "environment": "test",
                "period": {"start": "2026-10-01", "end": "2026-10-31"}, "source_complete": False,
                "files": entries}), encoding="utf-8")
            def parser(path, *, file_id):
                return {"file_id": file_id, "format": "docx", "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "blocks": [{"block_id": file_id + "-block", "text": file_id, "kind": "paragraph", "locator": {"paragraph": 1}, "metadata": {}}],
                        "coverage": [{"unit_id": "document", "state": "readable"}], "issues": []}
            source = LocalDocumentSource(root / "artifacts", parser=parser)
            spec = RunSpec("test", "001", "stop_work_unload", "2026-10", {"manifest": str(manifest)},
                           environment="test", callback_mode="none")
            events = []
            snapshot = source({"run_id": "run-1", "spec": spec.to_dict(), "catalog_profile": "oceanbase"},
                              progress_callback=events.append)
            started = [e["file_id"] for e in events if e.get("event") == "file_started"]
            self.assertEqual(["daily", "threshold", "dispatch", "design"], started)
            screening = next(e for e in events if e.get("stage") == "screening")
            self.assertEqual("provisional", screening["status"])
            self.assertEqual(2, screening["file_total"])
            self.assertEqual(4, len(snapshot["files"]))
            self.assertEqual(4, len(snapshot["evidence"]))
            self.assertEqual({entry["file_id"] for entry in entries}, {f["file_id"] for f in snapshot["files"]})


if __name__ == "__main__":
    unittest.main()
