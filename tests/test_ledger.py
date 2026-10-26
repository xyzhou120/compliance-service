"""Persistence, isolation and recovery tests against real SQLite transactions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from compliance_service import contracts
from compliance_service.contracts import Conflict, InvalidRequest, NotFound, RunSpec, StaleLease
from compliance_service.ledger import Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.now = 1000.0
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.spec = RunSpec("source", "mine-a", "stop_work_unload", "2026-10", {"fixture": "clean"})

    def start(self, spec=None):
        self.ledger.accept(spec or self.spec)
        return self.ledger.claim("worker")

    def snapshot(self, run):
        owner = {"run_id": run["run_id"], "mine_code": run["spec"]["mine_code"], "source_system": run["spec"]["source_system"], "environment": run["spec"]["environment"]}
        return {**owner, "snapshot_id": "snapshot-" + run["run_id"], "files": [{"content_hash": "hash"}], "source_complete": True,
                "evidence": [{**owner, "snapshot_id": "snapshot-" + run["run_id"], "content_hash": "hash", "evidence_id": "evidence-1"}]}

    def result(self, run, status="pass", snapshot=None):
        result = {"run_id": run["run_id"], "mine_code": run["spec"]["mine_code"], "itemCode": run["spec"]["item_code"], "checkCycle": run["spec"]["check_cycle"],
                  "definition_version": run["spec"]["definition_version"], "status": status, "description": "test result", "detail": "registered facts", "evidence_refs": []}
        if snapshot is not None:
            result.update(snapshot_id=snapshot["snapshot_id"], evidence_refs=["evidence-1"])
        return result

    def finish(self, status="pass", spec=None):
        run = self.start(spec)
        snapshot = self.snapshot(run)
        self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
        result = self.result(run, status, snapshot)
        self.ledger.finish(run["run_id"], "worker", run["fence"], result)
        return run, snapshot, result

    def get(self, run):
        return self.ledger.get(run["run_id"], self.spec.source_system, self.spec.mine_code, self.spec.environment)

    def test_twenty_parallel_accepts_share_one_active_run(self):
        barrier = threading.Barrier(20)
        def accept(_):
            barrier.wait()
            return Ledger(self.path).accept(self.spec)
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(accept, range(20)))
        self.assertEqual(1, len({r["run_id"] for r in results}))
        self.assertEqual(1, sum(r["created"] for r in results))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM runs").fetchone()[0])

    def test_running_scope_is_merged_but_different_callback_conflicts(self):
        run = self.start()
        accepted = self.ledger.accept(self.spec)
        self.assertFalse(accepted["created"])
        self.assertEqual("running", accepted["execution_state"])
        self.assertEqual(run["run_id"], accepted["run_id"])
        with self.assertRaises(Conflict):
            self.ledger.accept(replace(self.spec, callback_mode="none"))

    def test_each_terminal_code_allows_new_round_without_mutating_old(self):
        for code in contracts.RESULT_CODES:
            with self.subTest(code=code):
                run, snapshot, result = self.finish(code)
                self.assertEqual(snapshot, self.get(run)["snapshot"])
                self.assertEqual(result, self.get(run)["result"])
                expected = "failed" if code == "failed" else "completed"
                self.assertEqual(expected, self.get(run)["execution_state"])
                next_round = self.ledger.accept(self.spec)
                self.assertNotEqual(run["run_id"], next_round["run_id"])
                # End the next round so the following subtest has a clean scope.
                next_run = self.ledger.claim("worker")
                self.ledger.finish(next_run["run_id"], "worker", next_run["fence"], self.result(next_run, "failed"))
                self.assertEqual(result, self.get(run)["result"])

    def test_expired_worker_cannot_write_before_or_after_takeover(self):
        run = self.start()
        snapshot = self.snapshot(run)
        self.now += 30
        for operation in (lambda: self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot),
                          lambda: self.ledger.renew(run["run_id"], "worker", run["fence"]),
                          lambda: self.ledger.finish(run["run_id"], "worker", run["fence"], self.result(run, "failed"))):
            with self.assertRaises(StaleLease): operation()
        claimed = self.ledger.claim("new-worker")
        self.assertGreater(claimed["fence"], run["fence"])
        with self.assertRaises(StaleLease):
            self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)

    def test_renew_extends_current_lease(self):
        run = self.start()
        self.now += 20
        self.ledger.renew(run["run_id"], "worker", run["fence"])
        self.now += 20
        self.assertIsNone(self.ledger.claim("other"))
        self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], self.snapshot(run))

    def test_wrong_owner_or_fence_is_rejected(self):
        run = self.start()
        for worker, fence in (("other", run["fence"]), ("worker", run["fence"] + 1)):
            with self.assertRaises(StaleLease):
                self.ledger.renew(run["run_id"], worker, fence)

    def test_snapshot_ownership_and_evidence_references_are_required(self):
        run = self.start()
        snapshot = self.snapshot(run)
        for field in ("run_id", "mine_code", "source_system", "environment"):
            with self.subTest(field=field):
                bad = {**snapshot, field: "wrong"}
                with self.assertRaises(InvalidRequest):
                    self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], bad)
        snapshot["evidence"][0]["snapshot_id"] = "other"
        with self.assertRaises(InvalidRequest):
            self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
        self.assertIsNone(self.get(run)["snapshot"])

    def test_saved_snapshot_is_not_overwritable(self):
        run = self.start()
        snapshot = self.snapshot(run)
        self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
        with self.assertRaises(Conflict):
            self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)

    def test_queries_conceal_other_mines_sources_and_environments(self):
        run = self.start()
        for source, mine, environment in (("wrong", "mine-a", "local"), ("source", "wrong", "local"), ("source", "mine-a", "test")):
            with self.assertRaises(NotFound):
                self.ledger.get(run["run_id"], source, mine, environment)

    def test_validation_failure_does_not_finish_or_create_outbox(self):
        run = self.start()
        with self.assertRaises(InvalidRequest):
            self.ledger.finish(run["run_id"], "worker", run["fence"], self.result(run, "pass"))
        current = self.get(run)
        self.assertEqual("running", current["execution_state"])
        self.assertIsNone(current["result"])
        self.assertIsNone(self.ledger.claim_delivery("delivery"))

    def test_result_and_outbox_commit_or_rollback_together(self):
        run = self.start()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.finish(run["run_id"], "worker", run["fence"], self.result(run, "failed"))
        self.assertEqual("running", self.get(run)["execution_state"])
        self.assertIsNone(self.get(run)["result"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_failed_without_snapshot_and_no_callback_mode(self):
        run = self.start(replace(self.spec, callback_mode="none"))
        self.ledger.finish(run["run_id"], "worker", run["fence"], self.result(run, "failed"))
        self.assertEqual("none", self.get(run)["delivery_state"])
        self.assertIsNone(self.ledger.claim_delivery("delivery"))

    def test_delivery_retry_preserves_id_and_payload_and_has_backoff(self):
        run, _, result = self.finish()
        first = self.ledger.claim_delivery("delivery")
        self.assertEqual(result, first["payload"])
        self.ledger.complete_delivery(first["delivery_id"], "delivery", first["fence"], error="timeout")
        self.assertEqual("waiting", self.get(run)["delivery_state"])
        self.assertEqual("timeout", self.get(run)["delivery_error"])
        self.assertEqual(1, self.get(run)["delivery_attempt"])
        self.assertIsNone(self.ledger.claim_delivery("delivery"))
        self.now += 2
        retry = self.ledger.claim_delivery("delivery")
        self.assertEqual(first["delivery_id"], retry["delivery_id"])
        self.assertEqual(result, retry["payload"])
        self.assertEqual(2, retry["attempt"])
        self.ledger.complete_delivery(retry["delivery_id"], "delivery", retry["fence"])
        self.assertEqual("delivered", self.get(run)["delivery_state"])
        self.assertIsNone(self.get(run)["delivery_error"])
        self.assertIsNone(self.ledger.claim_delivery("delivery"))

    def test_delivery_timeout_fences_old_writer_and_deduplicates_receipt(self):
        self.finish()
        first = self.ledger.claim_delivery("old")
        self.ledger.record_mock_delivery(first["delivery_id"], first["payload"])
        self.now += 30
        with self.assertRaises(StaleLease):
            self.ledger.complete_delivery(first["delivery_id"], "old", first["fence"])
        retry = self.ledger.claim_delivery("new")
        with self.assertRaises(StaleLease):
            self.ledger.complete_delivery(first["delivery_id"], "old", first["fence"])
        self.ledger.record_mock_delivery(retry["delivery_id"], retry["payload"])
        self.ledger.complete_delivery(retry["delivery_id"], "new", retry["fence"])
        self.assertEqual(1, len(self.ledger.list_mock_receipts()))
        with self.assertRaises(Conflict):
            self.ledger.record_mock_delivery(retry["delivery_id"], {"status": "no_pass"})

    def test_reopened_sqlite_recovers_execution_snapshot_and_delivery(self):
        run = self.start()
        snapshot = self.snapshot(run)
        self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(snapshot, self.get(run)["snapshot"])
        self.now += 30
        recovered = self.ledger.claim("recovery")
        self.assertEqual(snapshot, recovered["snapshot"])
        result = self.result(recovered, "pass", snapshot)
        self.ledger.finish(recovered["run_id"], "recovery", recovered["fence"], result)
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        delivery = self.ledger.claim_delivery("delivery")
        self.assertEqual(result, delivery["payload"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(2, db.execute("PRAGMA user_version").fetchone()[0])

    def test_parent_directory_is_created_for_default_local_database(self):
        path = Path(self.temp.name) / "new-parent" / "nested" / "service.sqlite3"
        ledger = Ledger(path)
        self.assertTrue(ledger.accept(self.spec)["created"])
        self.assertTrue(path.exists())

    def test_progress_is_paginated_owner_scoped_and_persists_after_finish(self):
        run = self.start()
        events = [self.ledger.record_progress(run["run_id"], "worker", run["fence"],
                  {"stage": "recognition", "event": "provider_finished", "file_id": "f1",
                   "page": page, "provider": "ocr", "elapsed_ms": page * 100,
                   "status": "success", "issues": [], "headers": {"Authorization": "secret"}})
                  for page in range(1, 4)]
        first = self.ledger.get_progress(run["run_id"], "source", "mine-a", "local", limit=2)
        self.assertEqual([1, 2], [event["page"] for event in first["events"]])
        self.assertTrue(first["has_more"])
        self.assertNotIn("headers", first["events"][0])
        second = self.ledger.get_progress(run["run_id"], "source", "mine-a", "local",
                                          after=first["next_after"], limit=2)
        self.assertEqual([3], [event["page"] for event in second["events"]])
        self.assertFalse(second["has_more"])
        for owner in (("other", "mine-a", "local"), ("source", "mine-b", "local"),
                      ("source", "mine-a", "test")):
            with self.assertRaises(NotFound):
                self.ledger.get_progress(run["run_id"], *owner)
        snapshot = self.snapshot(run)
        self.ledger.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
        self.ledger.finish(run["run_id"], "worker", run["fence"], self.result(run, snapshot=snapshot))
        reopened = Ledger(self.path)
        progress = reopened.get_progress(run["run_id"], "source", "mine-a", "local")
        self.assertEqual("completed", progress["execution_state"])
        self.assertEqual("finished", progress["events"][-1]["event"])
        self.assertEqual(4, self.get(run)["progress_summary"]["event_count"])
        with self.assertRaises(StaleLease):
            reopened.record_progress(run["run_id"], "worker", run["fence"], {"event": "late"})

    def test_progress_fences_expired_and_replaced_workers(self):
        run = self.start()
        self.now += 30
        with self.assertRaises(StaleLease):
            self.ledger.record_progress(run["run_id"], "worker", run["fence"], {"event": "old"})
        replacement = self.ledger.claim("replacement")
        with self.assertRaises(StaleLease):
            self.ledger.record_progress(run["run_id"], "worker", run["fence"], {"event": "old"})
        current = self.ledger.record_progress(run["run_id"], "replacement", replacement["fence"],
                                               {"stage": "source", "event": "started"})
        self.assertEqual(replacement["fence"], current["fence"])

    def test_progress_bounds_and_old_schema_upgrade(self):
        run = self.start()
        for after, limit in ((-1, 100), (0, 0), (0, 501), (True, 1)):
            with self.assertRaises(InvalidRequest):
                self.ledger.get_progress(run["run_id"], "source", "mine-a", "local", after=after, limit=limit)
        for elapsed in (-1, float("inf"), float("nan"), True):
            with self.assertRaises(InvalidRequest):
                self.ledger.record_progress(run["run_id"], "worker", run["fence"], {"elapsed_ms": elapsed})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE run_progress")
            db.commit()
        upgraded = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual([], upgraded.get_progress(run["run_id"], "source", "mine-a", "local")["events"])
        self.assertIsNone(self.get(run)["result"])


if __name__ == "__main__":
    unittest.main()
