from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from compliance_service.contracts import Conflict, RunPaused, RunSpec, StaleLease
from compliance_service.ledger import Ledger


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000.0]
        self.ledger = Ledger(Path(self.temp.name) / 'service.sqlite3', clock=lambda: self.clock[0])
        self.spec = RunSpec('upstream', '001', 'stop_work_unload', '2026-10',
                            {'oceanbase': 'task-a'}, task_key='task-a', callback_mode='real',
                            callback_url='https://callbacks.example/result')
        self.task = {'task_key': 'task-a', 'raw': {'mineId': '001', 'status': 'running'},
                     'callback_url': self.spec.callback_url}

    def test_atomic_task_idempotency_and_callback_snapshot(self):
        first = self.ledger.accept_task(self.task, [self.spec])
        again = self.ledger.accept_task(self.task, [self.spec])
        self.assertEqual(first['items'][0]['run_id'], again['items'][0]['run_id'])
        self.assertFalse(again['created'])
        changed = {**self.task, 'callback_url': 'https://other.example/result'}
        with self.assertRaises(Conflict):
            self.ledger.accept_task(changed, [self.spec])
        self.assertEqual(self.task['raw'], self.ledger.get_task('task-a', 'upstream', '001', 'local')['raw'])

    def test_debug_only_worker_claims_only_local_simulator_tasks(self):
        self.ledger.accept_task(self.task, [self.spec])
        local = RunSpec('local', '001', 'stop_work_unload', '2026-10', {'fixture':'clean'},
                        environment='test', callback_mode='none', task_key='debug-queued')
        accepted = self.ledger.accept_task({'task_key':'debug-queued','raw':{'mineId':'001'},'callback_url':None}, [local])
        claimed = self.ledger.claim('local-worker', debug_only=True)
        self.assertEqual(accepted['items'][0]['run_id'], claimed['run_id'])
        self.assertIsNone(self.ledger.claim('another-local-worker', debug_only=True))
        self.assertIsNotNone(self.ledger.claim('upstream-worker'))

    def test_pause_at_checkpoint_and_resume_preserves_identity_and_fence(self):
        self.ledger.accept_task(self.task, [self.spec])
        run = self.ledger.claim('worker')
        self.ledger.control_task('task-a', 'upstream', '001', 'local', 'pause')
        with self.assertRaises(RunPaused):
            self.ledger.checkpoint(run['run_id'], 'worker', run['fence'])
        self.ledger.pause_run(run['run_id'], 'worker', run['fence'])
        self.assertIsNone(self.ledger.claim('other'))
        self.ledger.control_task('task-a', 'upstream', '001', 'local', 'resume')
        resumed = self.ledger.claim('new-worker')
        self.assertEqual(run['run_id'], resumed['run_id'])
        self.assertGreater(resumed['fence'], run['fence'])
        with self.assertRaises(StaleLease):
            self.ledger.checkpoint(run['run_id'], 'worker', run['fence'])

    def test_query_snapshot_is_immutable_before_download(self):
        self.ledger.accept_task(self.task, [self.spec])
        run = self.ledger.claim('worker')
        query = {'mine_code': '001', 'rows': [], 'query_version': 'v1'}
        self.ledger.save_acquisition(run['run_id'], 'worker', run['fence'], query)
        with self.assertRaises(Conflict):
            self.ledger.save_acquisition(run['run_id'], 'worker', run['fence'], {**query, 'rows': [1]})
        self.assertEqual(query, self.ledger.get(run['run_id'], 'upstream', '001', 'local')['acquisition'])

    def test_v1_migration_keeps_history_and_reopens_idempotently(self):
        path = Path(self.temp.name)/'v1.sqlite3'
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE runs(run_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,spec_json TEXT NOT NULL,created REAL NOT NULL,execution_state TEXT NOT NULL,worker_id TEXT,lease_until REAL,fence INTEGER NOT NULL DEFAULT 0,snapshot_json TEXT,result_json TEXT)')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)', ('historical','key','{}',10,'completed',None,None,2,None,'{"status":"pending"}'))
            db.execute('PRAGMA user_version=1')
            db.commit()
        finally:
            db.close()
        Ledger(path); Ledger(path)
        db = sqlite3.connect(path)
        try:
            self.assertEqual(2,db.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual('{"status":"pending"}',db.execute('SELECT result_json FROM runs').fetchone()[0])
            self.assertEqual(2,db.execute('SELECT execution_attempt FROM runs').fetchone()[0])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
