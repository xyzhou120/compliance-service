"""Local page-triggered tasks run their own inputs and never deliver callbacks."""
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from compliance_service.acquisition import AttachmentAcquisition
from compliance_service.api import create_app
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class DebugTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / 'service.sqlite3')
        self.service = InspectionService(self.ledger)
        self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)
        document = Document()
        document.add_paragraph('2026-10-14 403105工作面未记录动力现象。')
        buffer = io.BytesIO()
        document.save(buffer)
        self.data = buffer.getvalue()
        (self.root / 'daily.docx').write_bytes(self.data)
        self.manifest = {'source_system': 'sample-local', 'mine_code': '001', 'environment': 'test',
                         'period': {'start': '2026-10-01', 'end': '2026-10-31'}, 'source_complete': False,
                         'files': [{'file_id': 'daily', 'path': 'daily.docx', 'original_name': '日报.docx',
                                    'material_group': 'prevention_daily', 'role': 'actual_record'}]}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.body = {'mineId': '001', 'checkCycle': '2026-10', 'status': 'running',
                     'items': [{'itemCode': 'stop_work_unload'}], 'manifestPath': str(self.root / 'manifest.json'),
                     'resultCallbackUrl': 'http://127.0.0.1:1/must-not-be-called'}
        models = patch('compliance_service.config.load_model_settings', return_value={
            'ocr': {'enabled': False}, 'vision': {'enabled': False}})
        models.start()
        self.addCleanup(models.stop)

    def get(self, run_id, source='sample-local'):
        return self.ledger.get(run_id, source, '001', 'test')

    def test_page_task_auto_executes_native_documents_without_claiming_other_runs(self):
        older = self.service.submit({'sourceSystem':'other','mineCode':'002','checkCycle':'2026-10',
                                     'scope':{'fixture':'clean'},'callbackMode':'none',
                                     'items':[{'itemCode':'relief_effects'}]})['items'][0]['run_id']
        response = self.client.post('/v1/debug/upstream/tasks', json=self.body)
        self.assertEqual(202, response.status_code, response.text)
        accepted = response.json()
        run = self.get(accepted['items'][0]['run_id'])
        self.assertEqual('completed', run['execution_state'])
        self.assertEqual('none', run['delivery_state'])
        self.assertEqual('none', run['spec']['callback_mode'])
        self.assertEqual('日报.docx', run['snapshot']['files'][0]['original_name'])
        self.assertTrue(run['snapshot']['evidence'][0]['locator'])
        self.assertIn('未记录动力现象', run['snapshot']['evidence'][0]['text'])
        self.assertEqual('queued', self.ledger.get(older,'other','002','local')['execution_state'])
        self.assertIsNone(self.service.deliver_once())
        repeated = self.client.post('/v1/debug/upstream/tasks', json=self.body).json()
        self.assertNotEqual(accepted['items'][0]['run_id'], repeated['items'][0]['run_id'])

    def test_mismatched_owner_is_rejected_before_any_run_exists(self):
        for change in ({'mineId':'other'}, {'sourceSystem':'wrong'}, {'environment':'production'}):
            with self.subTest(change=change):
                response = self.client.post('/v1/debug/upstream/tasks', json={**self.body, **change})
                self.assertEqual(422,response.status_code,response.text)
        self.assertIsNone(self.service.run_once())

    def test_oceanbase_debug_works_with_no_callback_client(self):
        query = {'profile':'oceanbase-catalog','source_system':'oceanbase','mine_code':'001','environment':'test',
                 'snapshot_id':'query-1','period':self.manifest['period'],'query':{'query_version':'test-v1'},
                 'rows':[{'attachment_id':'daily','attachment_url':'https://files.test/daily.docx',
                          'attachment_name':'日报.docx','role':'actual_record','material_group':'prevention_daily'}]}
        class Repository:
            def query(self, mine, cycle, item): return deepcopy(query)
        self.service.repository = Repository()
        self.service.acquisition = AttachmentAcquisition(allowed_hosts={'files.test'},fetcher=lambda _:io.BytesIO(self.data))
        self.service.allowed_mines = {'001'}
        self.assertIsNone(self.service.callback_client)
        response = self.client.post('/v1/debug/upstream/tasks',json={**self.body,'dataSource':'oceanbase'})
        self.assertEqual(202,response.status_code,response.text)
        run = self.get(response.json()['items'][0]['run_id'],source='oceanbase')
        self.assertNotEqual('failed',run['execution_state'],run['result'])
        self.assertEqual('query-1',run['acquisition']['snapshot_id'])
        self.assertEqual('none',run['delivery_state'])
        self.assertIsNone(self.service.deliver_once())

    def test_configured_production_keeps_debug_endpoint_disabled(self):
        self.service.task_token = 'production-principal'
        self.assertEqual(404,self.client.post('/v1/debug/upstream/tasks',json=self.body).status_code)

    def test_missing_check_cycle_uses_current_month_fallback(self):
        with patch('compliance_service.contracts.default_check_cycle', return_value='2026-11'):
            response = self.client.post('/v1/debug/upstream/tasks', json={key: value for key, value in self.body.items() if key != 'checkCycle'})
        self.assertEqual(202, response.status_code, response.text)
        run = self.get(response.json()['items'][0]['run_id'])
        self.assertEqual('2026-11', run['spec']['check_cycle'])

    def test_external_worker_mode_persists_task_without_running_in_api(self):
        self.service.external_worker = True
        response = self.client.post('/v1/debug/upstream/tasks', json=self.body)
        self.assertEqual(202, response.status_code)
        run_id = response.json()['items'][0]['run_id']
        self.assertEqual('queued', self.get(run_id)['execution_state'])
        result = self.service.run_once(debug_only=True)
        self.assertEqual('pending', result['status'])
        self.assertEqual('completed', self.get(run_id)['execution_state'])


if __name__ == '__main__':
    unittest.main()
