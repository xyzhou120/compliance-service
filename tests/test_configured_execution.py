"""Configured codes and catalogs flow through real task execution, not just UI."""
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
from compliance_service.callbacks import URLPolicy
from compliance_service.contracts import InvalidRequest
from compliance_service.ledger import Ledger
from compliance_service.oceanbase import OceanBaseRepository
from compliance_service.service import InspectionService


class ConfiguredExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=InspectionService(Ledger(self.root/'service.sqlite3'))
        self.client=TestClient(create_app(self.service));self.addCleanup(self.client.close)
        models=patch('compliance_service.config.load_model_settings',return_value={'ocr':{'enabled':False},'vision':{'enabled':False}})
        models.start();self.addCleanup(models.stop)
        document=Document();document.add_paragraph('2026-10-14 403105工作面未记录动力现象。')
        output=io.BytesIO();document.save(output);self.data=output.getvalue()
        (self.root/'daily.docx').write_bytes(self.data)
        manifest={'source_system':'sample','mine_code':'001','environment':'test',
            'period':{'start':'2026-10-01','end':'2026-10-31'},'source_complete':False,
            'files':[{'file_id':'daily','path':'daily.docx','role':'actual_record','material_group':'prevention_daily'}]}
        self.manifest=self.root/'manifest.json';self.manifest.write_text(json.dumps(manifest),encoding='utf-8')
        self.task={'mineId':'001','checkCycle':'2026-10','status':'running',
            'items':[{'itemCode':'CJ001','itemName':'停工撤人与卸压'}],'manifestPath':str(self.manifest)}

    def save_custom(self):
        current=self.service.inspection_registry.read()
        config=current['config'];config['items'][0]['itemCode']='CJ001'
        config['query_policy']={'month_source':'fixed','fixed_month':'06'}
        return self.service.inspection_registry.save(config,expected_revision=current['revision'])

    def test_custom_code_native_document_execution_and_historical_identity(self):
        saved=self.save_custom()
        response=self.client.post('/v1/debug/upstream/tasks',json=self.task)
        self.assertEqual(202,response.status_code,response.text)
        run_id=response.json()['items'][0]['run_id']
        run=self.service.ledger.get(run_id,'sample','001','test')
        self.assertEqual('completed',run['execution_state'],run['result'])
        self.assertEqual('CJ001',run['result']['itemCode'])
        self.assertEqual('stop_work_unload',run['result']['rule_baseline']['item_code'])
        self.assertEqual(saved['revision'],run['snapshot']['inspection_configuration']['revision'])
        self.assertEqual('none',run['delivery_state'])
        before=run['result']
        updated=self.service.inspection_registry.read();updated['config']['items'][0]['itemCode']='CJ002'
        self.service.inspection_registry.save(updated['config'],expected_revision=updated['revision'])
        self.assertEqual(before,self.service.ledger.get(run_id,'sample','001','test')['result'])
        self.assertEqual(422,self.client.post('/v1/debug/upstream/tasks',json=self.task).status_code)

    def test_queued_oceanbase_task_uses_frozen_catalog_and_month_after_config_edit(self):
        saved=self.save_custom()
        original=saved['config']['items'][0]['materials'][0]['file_name_id']
        calls=[]
        class Cursor:
            description=[]
            def execute(self,sql,params):calls.append((sql,params))
            def fetchall(_):return [{'mine_code':'001','attachment_id':'daily','file_name_id':original,
                'attachment_name':'日报.docx','attachment_url':'https://files.test/a.docx',
                'report_time_year':2026,'report_time_month':'10','report_date':'2026/10/00'}]
            def close(self):pass
        class Connection:
            def cursor(self):return Cursor()
            def close(self):pass
        self.service.repository=OceanBaseRepository(Connection)
        self.service.acquisition=AttachmentAcquisition(allowed_hosts={'files.test'},fetcher=lambda _:io.BytesIO(self.data))
        self.service.allowed_mines={'001'}
        accepted=self.service.submit_debug_task({**self.task,'dataSource':'oceanbase'})
        changed=self.service.inspection_registry.read();item=changed['config']['items'][0]
        item['itemCode']='CJ002';item['materials'][0]['file_name_id']='NEW_ENVIRONMENT_ID'
        changed['config']['query_policy']={'month_source':'fixed','fixed_month':'07'}
        self.service.inspection_registry.save(changed['config'],expected_revision=changed['revision'])
        self.service.run_once(run_id=accepted['items'][0]['run_id'])
        run=self.service.ledger.get(accepted['items'][0]['run_id'],'oceanbase','001','test')
        self.assertEqual('completed',run['execution_state'],run['result'])
        self.assertIn(original,calls[0][1]);self.assertNotIn('NEW_ENVIRONMENT_ID',calls[0][1])
        self.assertEqual('10',run['acquisition']['query']['query_policy']['resolved_month'])
        self.assertEqual('2026-10-01',run['acquisition']['period']['start'])
        self.assertEqual('2026-10',run['result']['checkCycle'])
        self.assertEqual('CJ001',run['result']['itemCode'])
        self.assertEqual(saved['revision'],run['acquisition']['inspection_config_revision'])

    def test_custom_fixture_code_and_disabled_item_are_enforced(self):
        self.save_custom()
        body={'sourceSystem':'sample','mineCode':'001','checkCycle':'2026-10','environment':'test',
              'scope':{'fixture':'clean'},'callbackMode':'none','items':[{'itemCode':'CJ001'}]}
        run_id=self.service.submit(body)['items'][0]['run_id']
        self.assertEqual('pass',self.service.run_once(run_id=run_id)['status'])
        current=self.service.inspection_registry.read();current['config']['items'][0]['enabled']=False
        self.service.inspection_registry.save(current['config'],expected_revision=current['revision'])
        with self.assertRaises(InvalidRequest):self.service.submit(body)
        reopened=InspectionService(self.service.ledger)
        with self.assertRaises(InvalidRequest):reopened.submit(body)

    def test_existing_task_can_pause_and_resume_after_code_changes(self):
        self.save_custom()
        self.service.repository=object()
        self.service.acquisition=object()
        self.service.callback_client=type('Callback',(),{'policy':URLPolicy(allowed_hosts={'callback.test'})})()
        self.service.status_map={'paused':'pause','resumed':'resume'}
        body={'mineId':'001','status':'running','checkCycle':'2026-10',
              'items':[{'itemCode':'CJ001','itemName':'停工撤人与卸压'}],
              'resultCallbackUrl':'https://callback.test/result'}
        accepted=self.service.receive_task(body,allowed_mines={'001'})
        current=self.service.inspection_registry.read();current['config']['items'][0]['itemCode']='CJ002'
        self.service.inspection_registry.save(current['config'],expected_revision=current['revision'])
        paused=self.service.receive_task({**body,'status':'paused'},allowed_mines={'001'})
        self.assertEqual(accepted['task_key'],paused['task_key'])
        self.assertEqual('paused',self.service.ledger.get(accepted['items'][0]['run_id'],'oceanbase','001','test')['execution_state'])
        self.service.receive_task({**body,'status':'resumed'},allowed_mines={'001'})
        stored=self.service.ledger.get(accepted['items'][0]['run_id'],'oceanbase','001','test')
        self.assertEqual('queued',stored['execution_state'])
        self.assertEqual('CJ001',stored['spec']['item_code'])


if __name__=='__main__':unittest.main()
