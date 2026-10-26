from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from compliance_service.contracts import accepted_inspection_item, default_check_cycle, resolve_check_cycle
from compliance_service.inspection_config import default_config
from compliance_service.oceanbase import OceanBaseRepository
from compliance_service.upstream import normalize_upstream_task
from tests.test_oceanbase_acquisition import Conn


class CyclePriorityTests(unittest.TestCase):
    def test_default_uses_business_timezone_at_month_boundary(self):
        class Boundary(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 31, 18, tzinfo=timezone.utc).astimezone(tz)
        with patch("compliance_service.contracts.datetime", Boundary):
            self.assertEqual("2026-11", default_check_cycle())

    def test_only_absent_cycle_falls_back(self):
        with patch("compliance_service.contracts.default_check_cycle", return_value="2026-11"):
            self.assertEqual("2026-11", resolve_check_cycle(None))
            self.assertEqual("2026-09", resolve_check_cycle("2026-09"))
            self.assertEqual("", resolve_check_cycle(""))

    def test_new_admission_ignores_legacy_current_or_fixed_month_policy(self):
        for policy in ({"month_source":"current_month"}, {"month_source":"fixed","fixed_month":"06"}):
            config=default_config();config["query_policy"]=policy
            accepted=accepted_inspection_item(config,"stop_work_unload","2026-09")
            self.assertEqual("09",accepted["resolved_query_month"])
            self.assertEqual({"month_source":"task_month"},accepted["query_policy"])
            queried=OceanBaseRepository(lambda:Conn([]),inspection_config=config).query("001","2026-09","stop_work_unload")
            self.assertEqual("2026-09-01",queried["period"]["start"])

    def test_legacy_frozen_snapshot_keeps_query_range_and_policy(self):
        item=default_config()["items"][0]
        frozen={**item,"query_policy":{"month_source":"fixed","fixed_month":"06"},
                "resolved_query_month":"06","revision":"old-accepted-revision"}
        queried=OceanBaseRepository(lambda:Conn([])).query("001","2026-09","stop_work_unload",inspection_config=frozen)
        self.assertEqual("2026-06-01",queried["period"]["start"])
        self.assertEqual("fixed",queried["query"]["query_policy"]["month_source"])

    def test_missing_upstream_and_direct_query_cycle_use_same_resolved_month(self):
        body={"mineId":"001","status":"running","items":[{"itemCode":"stop_work_unload"}],
              "resultCallbackUrl":"https://callback.test/result"}
        with patch("compliance_service.contracts.default_check_cycle", return_value="2026-11"):
            normalized=normalize_upstream_task(body)
            queried=OceanBaseRepository(lambda:Conn([])).query("001",None,"stop_work_unload")
        self.assertEqual("2026-11",normalized["check_cycle"])
        self.assertEqual("2026-11-01",queried["period"]["start"])
        self.assertNotIn("checkCycle",normalized["raw_payload"])
