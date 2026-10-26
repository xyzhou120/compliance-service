"""Execution history groups task items and reopens persisted results."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.contracts import RunSpec
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class ExecutionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'history.sqlite3'
        self.clock = [1000.0]
        self.ledger = Ledger(self.path, clock=lambda:self.clock[0])
        self.service = InspectionService(self.ledger)
        self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)

    def task(self, key, mine='001', environment='test'):
        specs=[RunSpec('local-source',mine,code,'2026-10',{'fixture':'clean'},
                       environment=environment,callback_mode='none',task_key=key)
               for code in ('stop_work_unload','relief_effects')]
        return self.ledger.accept_task({'task_key':key,'raw':{},'callback_url':None},specs)

    def test_grouped_items_status_pagination_filters_and_persistence(self):
        first=self.task('first')
        self.service.run_once(run_id=first['items'][0]['run_id'])
        self.clock[0]+=10
        self.task('second',mine='002')
        self.task('production',mine='003',environment='production')
        response=self.client.get('/v1/debug/executions',params={'limit':1})
        self.assertEqual(200,response.status_code,response.text)
        page=response.json()
        self.assertEqual(2,page['total'])
        self.assertEqual('second',page['records'][0]['record_id'])
        self.assertEqual(2,len(page['records'][0]['items']))
        self.assertTrue(page['has_more'])
        second=self.client.get('/v1/debug/executions',params={'offset':1,'limit':1}).json()
        self.assertEqual('first',second['records'][0]['record_id'])
        self.assertEqual('queued',second['records'][0]['execution_state'])
        self.assertEqual(1,second['records'][0]['completed_count'])
        self.assertFalse(second['has_more'])
        self.assertEqual('pass',second['records'][0]['items'][0]['result_status'])
        self.assertTrue(all('snapshot' not in i and 'result' not in i for i in second['records'][0]['items']))
        filtered=self.client.get('/v1/debug/executions',params={'mine_code':'001','check_cycle':'2026-10'}).json()
        self.assertEqual(1,filtered['total'])
        reopened=Ledger(self.path,clock=lambda:self.clock[0])
        self.assertEqual(2,reopened.list_executions()['total'])
        selected=filtered['records'][0]
        item=next(i for i in selected['items'] if i['result_status']=='pass')
        run=self.client.get('/v1/runs/'+item['run_id'],params={
            'source_system':selected['source_system'],'mine_code':selected['mine_code'],
            'environment':selected['environment']})
        self.assertEqual(200,run.status_code)
        self.assertEqual('pass',run.json()['result']['status'])

    def test_legacy_runs_are_included_and_failed_is_distinct_from_business_no_pass(self):
        accepted=self.service.submit({'sourceSystem':'legacy','mineCode':'old','checkCycle':'2026-09',
            'scope':{'fixture':'violation'},'callbackMode':'none','items':[{'itemCode':'stop_work_unload'}]})
        self.service.run_once()
        record=self.client.get('/v1/debug/executions').json()['records'][0]
        self.assertEqual(accepted['items'][0]['run_id'],record['record_id'])
        self.assertEqual('completed',record['execution_state'])
        self.assertEqual('no_pass',record['items'][0]['result_status'])

    def test_invalid_pagination_and_production_access(self):
        for params in ({'limit':0},{'limit':101},{'offset':-1}):
            self.assertEqual(422,self.client.get('/v1/debug/executions',params=params).status_code)
        self.service.task_token='configured-production-token'
        self.assertEqual(404,self.client.get('/v1/debug/executions').status_code)

    def test_running_failed_and_empty_page_states(self):
        accepted=self.task('running')
        claimed=self.ledger.claim('worker',run_id=accepted['items'][0]['run_id'])
        self.assertIsNotNone(claimed)
        self.assertEqual('running',self.ledger.list_executions()['records'][0]['execution_state'])
        self.clock[0]+=40
        self.service.run_once(run_id=accepted['items'][0]['run_id'])
        self.service.run_once(run_id=accepted['items'][1]['run_id'])
        self.assertEqual('completed',self.ledger.list_executions()['records'][0]['execution_state'])
        empty=self.ledger.list_executions(offset=100)
        self.assertEqual([],empty['records'])
        self.assertEqual(1,empty['total'])
        self.assertFalse(empty['has_more'])
        failed=self.service.submit({'sourceSystem':'legacy','mineCode':'old','checkCycle':'2026-09',
            'scope':{'fixture':'source_error'},'callbackMode':'none','items':[{'itemCode':'stop_work_unload'}]})
        self.service.run_once(run_id=failed['items'][0]['run_id'])
        record=self.ledger.list_executions()['records'][0]
        self.assertEqual('failed',record['execution_state'])
        self.assertEqual('failed',record['items'][0]['result_status'])


if __name__=='__main__': unittest.main()
