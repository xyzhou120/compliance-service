"""Vision budget and bounded region repair."""

import unittest
from io import BytesIO

from PIL import Image

from compliance_service.canonical import Cell, Locator, Table
from compliance_service.repair import repair_region
from compliance_service.vision_budget import VisionBudget


def png(width=400, height=300):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


class FakeVision:
    def __init__(self, table=None, status="table_verified", raise_error=None):
        self.table = table
        self.status = status
        self.raise_error = raise_error
        self.calls = 0
        self.timeout = 180

    def transcribe_table(self, image, *, file_id, page, file_hash=None):
        self.calls += 1
        if self.raise_error:
            raise self.raise_error
        if self.table is None:
            return None, {"status": self.status, "reasons": ["SPARSE_CELLS"]}
        return self.table, {"status": self.status, "reasons": []}


def simple_table():
    locator = Locator(file_id="f1", page=1, table=1)
    rows = ((Cell("方法", locator), Cell("值", locator)),
            (Cell("应力", locator), Cell("10", locator)))
    return Table(table_id="t1", rows=rows, locator=locator, header_rows=1)


class BudgetTests(unittest.TestCase):
    def test_per_page_and_batch_limits_are_enforced(self):
        budget = VisionBudget(per_page=1, per_batch=3)
        allowed, _ = budget.allow("p1")
        self.assertTrue(allowed)
        budget.consume("p1")
        allowed, reason = budget.allow("p1")
        self.assertFalse(allowed)
        self.assertEqual("PER_PAGE_BUDGET", reason)
        budget.consume("p2")
        budget.consume("p3")
        allowed, reason = budget.allow("p4")
        self.assertFalse(allowed)
        self.assertEqual("BATCH_BUDGET", reason)
        self.assertEqual(3, budget.summary()["used"])


class RepairTests(unittest.TestCase):
    def test_repair_returns_verified_table(self):
        budget = VisionBudget()
        vision = FakeVision(simple_table())
        result = repair_region(vision, budget, png(), [10, 10, 300, 200], file_id="f1", page=1)
        self.assertEqual("table_verified", result["status"])
        self.assertIsNotNone(result["table"])
        self.assertEqual(1, budget.summary()["used"])

    def test_budget_exhaustion_skips_without_calling_the_model(self):
        budget = VisionBudget(per_page=0)
        vision = FakeVision(simple_table())
        result = repair_region(vision, budget, png(), [10, 10, 300, 200], file_id="f1", page=1)
        self.assertEqual("VISION_BUDGET_SKIPPED", result["status"])
        self.assertEqual(0, vision.calls)

    def test_model_failure_is_recorded_not_raised(self):
        budget = VisionBudget()
        vision = FakeVision(raise_error=TimeoutError("slow"))
        result = repair_region(vision, budget, png(), [10, 10, 300, 200], file_id="f1", page=1)
        self.assertEqual("VISION_FAILED", result["status"])
        self.assertEqual("TimeoutError", result["reason"])
        self.assertIsNone(result["table"])

    def test_bad_region_does_not_consume_budget(self):
        budget = VisionBudget()
        vision = FakeVision(simple_table())
        result = repair_region(vision, budget, png(10, 10), [0, 0, 5, 5], file_id="f1", page=1)
        self.assertEqual("CROP_FAILED", result["status"])
        self.assertEqual(0, budget.summary()["used"])


if __name__ == "__main__":
    unittest.main()
