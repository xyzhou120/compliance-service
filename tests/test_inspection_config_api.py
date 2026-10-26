"""Local configuration editing changes advertised item codes, never results."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.inspection_config import InspectionConfigRegistry
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class InspectionConfigApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = InspectionService(Ledger(root / 'ledger.sqlite3'))
        self.service.inspection_registry = InspectionConfigRegistry(root / 'inspection-config.json')
        self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)

    def test_get_save_reload_and_advertised_codes(self):
        response = self.client.get('/v1/debug/inspection-config')
        self.assertEqual(200, response.status_code, response.text)
        original = response.json()
        config = deepcopy(original['config'])
        config['items'][0]['itemCode'] = 'upstream-101'
        config['items'][0]['name'] = '停工撤人与卸压'
        config['items'][0]['materials'][0]['file_name_id'] = 'env-b-prevention-id'
        config['items'][0]['materials'][0]['cadence'] = 'semiannual'
        response = self.client.post('/v1/debug/inspection-config', json={
            'config': config, 'expectedRevision': original['revision']})
        self.assertEqual(200, response.status_code, response.text)
        saved = response.json()
        self.assertNotEqual(original['revision'], saved['revision'])
        self.assertEqual(config, InspectionConfigRegistry(self.service.inspection_registry.path).read()['config'])
        options = self.client.get('/v1/debug/task-options').json()
        self.assertEqual(config, options['inspection_config'])
        self.assertEqual(saved['revision'], options['inspection_config_revision'])
        definitions = self.client.get('/v1/definitions').json()['items']
        self.assertIn('upstream-101', {i['itemCode'] for i in definitions})
        self.assertNotIn('stop_work_unload', {i['itemCode'] for i in definitions})
        groups = self.client.get('/v1/debug/material-groups').json()['groups']
        self.assertIn('upstream-101', groups[0]['items'])

    def test_invalid_and_stale_save_preserve_existing_configuration(self):
        original = self.client.get('/v1/debug/inspection-config').json()
        bad = deepcopy(original['config'])
        bad['items'][0]['materials'][0]['cadence'] = 'unsupported'
        response = self.client.post('/v1/debug/inspection-config', json={
            'config': bad, 'expectedRevision': original['revision']})
        self.assertEqual(422, response.status_code)
        self.assertEqual(original['revision'], self.client.get('/v1/debug/inspection-config').json()['revision'])
        changed = deepcopy(original['config'])
        changed['query_policy']['month_source'] = 'task_month'
        self.assertEqual(200, self.client.post('/v1/debug/inspection-config', json={
            'config': changed, 'expectedRevision': original['revision']}).status_code)
        stale = self.client.post('/v1/debug/inspection-config', json={
            'config': original['config'], 'expectedRevision': original['revision']})
        self.assertIn(stale.status_code, (409, 422))
        self.assertEqual('task_month', self.client.get('/v1/debug/inspection-config').json()['config']['query_policy']['month_source'])

    def test_save_requires_revision_and_rejects_unknown_fields(self):
        original = self.client.get('/v1/debug/inspection-config').json()
        for body in ({'config':original['config']},
                     {'config':original['config'],'expectedRevision':''},
                     {'config':original['config'],'expectedRevision':original['revision'],'callbackMode':'real'}):
            with self.subTest(body=body):
                self.assertEqual(422,self.client.post('/v1/debug/inspection-config',json=body).status_code)
        self.assertEqual(original['revision'],self.client.get('/v1/debug/inspection-config').json()['revision'])

    def test_production_configuration_management_is_unavailable(self):
        self.service.task_token = 'production-only-token'
        self.assertEqual(404, self.client.get('/v1/debug/inspection-config').status_code)
        self.assertEqual(404, self.client.post('/v1/debug/inspection-config', json={'config': {}}).status_code)

    def test_page_exposes_configuration_and_dynamic_items(self):
        text = self.client.get('/debug/legacy').text
        self.assertIn('检查项与清单配置', text)
        self.assertIn('OceanBase 清单 ID', text)
        self.assertIn('debug-configured-items', text)
        self.assertIn('/v1/debug/inspection-config', text)
        self.assertIn('supportedInspectionItems', text)


if __name__ == '__main__':
    unittest.main()
