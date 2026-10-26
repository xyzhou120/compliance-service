import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from compliance_service.contracts import Conflict, InvalidRequest
from compliance_service.inspection_config import (
    InspectionConfigRegistry, default_config, find_item, load_config, revision, validate_config,
)


class InspectionConfigTests(unittest.TestCase):
    def test_defaults_are_configurable_and_preserve_current_items(self):
        config = default_config()
        self.assertEqual({"stop_work_unload", "relief_effects"}, {i["itemCode"] for i in config["items"]})
        item = find_item(config, "stop_work_unload")
        self.assertEqual("冲击危险停工撤人与卸压", item["name"])
        self.assertEqual("annual", next(m["cadence"] for m in item["materials"] if m["material_group"] == "threshold"))
        self.assertEqual("task_month", config["query_policy"]["month_source"])

    def test_rejects_bad_item_material_and_period_configuration(self):
        base = default_config()
        cases = []
        cases.append({**base, "items": [{**base["items"][0], "itemCode": ""}]})
        cases.append({**base, "items": [{**base["items"][0], "handler": "unknown"}]})
        cases.append({**base, "items": [{**base["items"][0], "materials": [{**base["items"][0]["materials"][0], "cadence": "quarterly"}]}]})
        cases.append({**base, "query_policy": {**base["query_policy"], "month_source": "fixed", "fixed_month": "13"}})
        cases.append({**base, "items": [{**base["items"][0], "materials": [{**base["items"][0]["materials"][0], "material_group": "arbitrary"}]}]})
        for case in cases:
            with self.subTest(case=case), self.assertRaises(InvalidRequest):
                validate_config(case)

    def test_registry_persists_atomically_and_detects_stale_revision(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "inspection.json"
            registry = InspectionConfigRegistry(path)
            first = registry.read()
            self.assertFalse(path.exists())
            changed = json.loads(json.dumps(first["config"]))
            changed["items"][0]["name"] = "已修改检查项"
            saved = registry.save(changed, expected_revision=first["revision"])
            self.assertTrue(path.exists())
            self.assertEqual(revision(changed), saved["revision"])
            with self.assertRaises(Conflict):
                registry.save(first["config"], expected_revision=first["revision"])
            self.assertEqual("task_month", registry.read()["config"]["query_policy"]["month_source"])

    def test_custom_item_code_is_used_directly_without_alias_mapping(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "inspection.json"
            config = default_config()
            config["items"][0]["itemCode"] = "CJ001"
            config["items"][0]["name_aliases"] = ["停工撤人与卸压"]
            registry = InspectionConfigRegistry(path)
            registry.save(config)
            self.assertEqual("CJ001", find_item(registry.read()["config"], "CJ001")["itemCode"])
            with self.assertRaises(InvalidRequest):
                find_item(registry.read()["config"], "stop_work_unload")

    def test_load_missing_path_uses_defaults_without_writing(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            loaded = load_config(path)
            self.assertEqual("stop_work_unload", loaded["items"][0]["itemCode"])
            self.assertFalse(path.exists())

    def test_concurrent_stale_writers_cannot_overwrite_each_other(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "inspection.json"
            registry = InspectionConfigRegistry(path)
            initial = registry.read()
            barrier = Barrier(2)
            def save(name):
                config = json.loads(json.dumps(initial["config"]))
                config["items"][0]["name"] = name
                barrier.wait()
                try:
                    return registry.save(config, initial["revision"])["config"]["items"][0]["name"]
                except Conflict:
                    return "conflict"
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(save, ["first", "second"]))
            self.assertEqual(1, results.count("conflict"))
            self.assertIn(registry.read()["config"]["items"][0]["name"], results)

    def test_structurally_invalid_values_raise_request_errors(self):
        for path, value in (("schema_version", True), ("month_source", []), ("handler", []), ("role", []), ("cadence", [])):
            config = default_config()
            if path == "schema_version": config[path] = value
            elif path == "month_source": config["query_policy"][path] = value
            elif path == "handler": config["items"][0][path] = value
            else: config["items"][0]["materials"][0][path] = value
            with self.subTest(path=path), self.assertRaises(InvalidRequest):
                validate_config(config)
        config = default_config()
        config["query_policy"] = {"month_source": "fixed", "fixed_month": True}
        with self.assertRaises(InvalidRequest): validate_config(config)

    def test_invalid_save_keeps_previous_configuration_and_disabled_item_rejects(self):
        with TemporaryDirectory() as td:
            registry = InspectionConfigRegistry(Path(td)/"inspection.json")
            initial = registry.read()
            bad = {**initial["config"], "unexpected": "field"}
            with self.assertRaises(InvalidRequest): registry.save(bad, initial["revision"])
            self.assertEqual(initial["revision"], registry.read()["revision"])
            disabled = initial["config"]
            disabled["items"][0]["enabled"] = False
            registry.save(disabled, initial["revision"])
            with self.assertRaises(InvalidRequest): registry.get_item("stop_work_unload")


if __name__ == "__main__":
    unittest.main()
