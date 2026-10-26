import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.contracts import RunSpec
from compliance_service.debug_views import register_debug_views
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class DebugViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / "ledger.sqlite3")
        self.service = InspectionService(self.ledger)
        self.service_app = create_app(self.service)
        register_debug_views(self.service_app, self.service)
        self.client = TestClient(self.service_app); self.addCleanup(self.client.close)

    def submit(self, item="stop_work_unload"):
        return self.service.submit({"sourceSystem":"debug","mineCode":"001","environment":"test",
            "checkCycle":"2026-10","scope":{"fixture":"clean"},"callbackMode":"none",
            "items":[{"itemCode":item}]})["items"][0]["run_id"]

    def test_report_files_evidence_and_diagnostics_are_paged_without_snapshot_leak(self):
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        self.assertEqual(200,self.client.get(f"/v1/debug/runs/{run_id}/report").status_code)
        report=self.client.get(f"/v1/debug/runs/{run_id}/report").json()
        self.assertEqual("pass",report["status"])
        self.assertNotIn("snapshot",report); self.assertNotIn("evidence",report)
        self.assertEqual(200,self.client.get(f"/v1/debug/runs/{run_id}/files").status_code)
        files=self.client.get(f"/v1/debug/runs/{run_id}/files?limit=1").json()
        self.assertTrue(files["total"] >= 1)
        evidence=self.client.get(f"/v1/debug/runs/{run_id}/evidence?limit=1").json()
        self.assertTrue(evidence["total"] >= 1)
        self.assertTrue(self.client.get(f"/v1/debug/runs/{run_id}/evidence?file_id=synthetic-source").json()["items"])
        self.assertTrue(self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=propositions").json()["items"])
        self.assertEqual(200,self.client.get(f"/v1/debug/runs/{run_id}/progress").status_code)

    def test_task_detail_and_pagination_validation(self):
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        detail=self.client.get("/v1/debug/tasks/" + run_id).json()
        self.assertEqual(run_id,detail["record_id"])
        self.assertEqual(1,detail["item_count"])
        self.assertEqual(422,self.client.get(f"/v1/debug/runs/{run_id}/files?limit=0").status_code)
        self.assertEqual(422,self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=unknown").status_code)

    def test_running_query_scope_prefers_frozen_range_and_keeps_requested_cycle(self):
        run_id=self.submit()
        with self.ledger._tx() as db:
            spec=json.loads(db.execute("SELECT spec_json FROM runs WHERE run_id=?",(run_id,)).fetchone()[0])
            spec["check_cycle"]="2026-09"
            spec["inspection_item"]={"query_policy":{"month_source":"task_month"},"resolved_query_month":"09"}
            acquisition={"period":{"start":"2026-10-01","end":"2026-10-31"},
                         "query":{"query_policy":{"month_source":"current_month","resolved_month":"10"}},"rows":[]}
            db.execute("UPDATE runs SET execution_state='running',spec_json=?,acquisition_json=? WHERE run_id=?",
                       (json.dumps(spec),json.dumps(acquisition),run_id))
        task=self.client.get("/v1/debug/tasks/"+run_id).json()
        scope=task["items"][0]["query_scope"]
        self.assertEqual("2026-09",scope["requested_cycle"])
        self.assertEqual("2026-10",scope["resolved_cycle"])
        self.assertEqual("2026-10-31",scope["period"]["end"])
        self.assertEqual("frozen_query",scope["source"])
        self.assertTrue(scope["differs_from_task"])
        self.assertIsNone(task["items"][0]["source_complete"])
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET acquisition_json=NULL WHERE run_id=?",(run_id,))
        accepted=self.client.get("/v1/debug/tasks/"+run_id).json()["items"][0]["query_scope"]
        self.assertEqual("2026-09",accepted["resolved_cycle"])
        self.assertEqual("2026-09-30",accepted["period"]["end"])
        self.assertFalse(accepted["differs_from_task"])

    def test_running_download_and_file_progress_are_visible_before_final_snapshot(self):
        run_id=self.submit()
        acquisition={"rows":[{"attachment_id":f"f-{i}","attachment_name":f"日报{i}.pdf"} for i in range(4)]}
        events=[
            {"stage":"acquisition","event":"attachment_finished","file_id":"f-0","status":"failed"},
            {"stage":"acquisition","event":"attachment_finished","file_id":"f-0","status":"completed"},
            {"stage":"acquisition","event":"attachment_finished","file_id":"f-1","status":"completed"},
            {"stage":"acquisition","event":"attachment_finished","file_id":"f-2","status":"failed","issues":["TimeoutError"]},
            {"stage":"source","event":"source_started","file_total":2},
            {"stage":"source","event":"file_started","file_id":"f-0","file_name":"日报0.pdf"},
            {"stage":"source","event":"file_finished","file_id":"f-0","status":"acquired"},
            {"stage":"source","event":"file_started","file_id":"f-1","file_name":"日报1.pdf"},
            {"stage":"recognition","event":"page_started","file_id":"f-1","page":2,"page_total":5},
            {"stage":"recognition","event":"page_finished","file_id":"f-1","page":2,"status":"failed"},
            {"stage":"recognition","event":"page_started","file_id":"f-1","page":3,"page_total":5},
        ]
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET execution_state='running',acquisition_json=? WHERE run_id=?",(json.dumps(acquisition),run_id))
            for index,event in enumerate(events):
                db.execute("INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",(run_id,index,1,json.dumps(event)))
        with patch.object(self.ledger,"get",side_effect=AssertionError("summary must not deserialize snapshot")):
            task=self.client.get("/v1/debug/tasks/"+run_id)
            files=self.client.get(f"/v1/debug/runs/{run_id}/files?limit=2").json()
        self.assertLess(len(task.content),10000)
        item=task.json()["items"][0]
        self.assertIsNone(item["source_complete"])
        progress=item["progress"]
        self.assertEqual(4,progress["catalog_files"])
        self.assertEqual(2,progress["downloaded"])
        self.assertEqual(1,progress["download_failed"])
        self.assertEqual(1,progress["processed_files"])
        self.assertEqual(2,progress["processing_files"])
        self.assertEqual("日报1.pdf",progress["current_file"]["file_name"])
        self.assertEqual(3,progress["current_file"]["page"])
        self.assertEqual(1,progress["recognition_pages_failed"])
        self.assertEqual(["acquired","running"],[file["status"] for file in files["items"]])
        remaining=self.client.get(f"/v1/debug/runs/{run_id}/files?offset=2&limit=2").json()
        self.assertEqual(["failed","queued"],[file["status"] for file in remaining["items"]])
        self.assertEqual("TimeoutError",remaining["items"][0]["error"])

    def test_failed_report_locates_last_started_page_without_rewriting_result(self):
        run_id=self.submit()
        result={"status":"failed","description":"检查执行技术失败","detail":"DecompressionBombError",
                "error_type":"DecompressionBombError","error_stage":"source"}
        acquisition={"rows":[{"attachment_id":"file-1","attachment_name":"工作面评价报告.pdf"}]}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET execution_state='failed',result_json=?,acquisition_json=? WHERE run_id=?",
                       (json.dumps(result),json.dumps(acquisition,ensure_ascii=False),run_id))
            db.execute("INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",
                       (run_id,1,1,json.dumps({"stage":"recognition","event":"page_started","file_id":"file-1","page":2})))
            db.execute("INSERT INTO run_progress(run_id,created,fence,event_json) VALUES(?,?,?,?)",
                       (run_id,2,1,json.dumps({"stage":"source","event":"failed","issues":["DecompressionBombError"]})))
        report=self.client.get(f"/v1/debug/runs/{run_id}/report").json()
        self.assertEqual("工作面评价报告.pdf",report["failure_location"]["file_name"])
        self.assertEqual("file-1",report["failure_location"]["file_id"])
        self.assertEqual(2,report["failure_location"]["page"])
        self.assertEqual("DecompressionBombError",report["error_type"])
        self.assertIsNone(self.ledger.get(run_id,"debug","001","test")["snapshot"])

    def test_finding_date_comes_from_its_own_event_only(self):
        run_id=self.submit();self.service.run_once(run_id=run_id)
        snapshot={"files":[],"facts":[{"kind":"event","event_id":"E1","workface":"W1","time":"2026-10-14T08:00:00"},
                    {"kind":"event","event_id":"E2","workface":"W2","time":"2026-10-15T08:00:00"}]}
        result={"status":"no_pass","description":"问题","detail":"依据", "findings":[{"event_id":"E1","workface":"W1","code":"stop"}]}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=?,result_json=? WHERE run_id=?",(json.dumps(snapshot),json.dumps(result),run_id))
        report=self.client.get(f"/v1/debug/runs/{run_id}/report").json()
        self.assertEqual('2026-10-14T08:00:00',report['findings'][0]['event_time'])
        snapshot['facts'].append({"kind":"event","event_id":"E1","workface":"W1","time":"2026-10-16T08:00:00"})
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=? WHERE run_id=?",(json.dumps(snapshot),run_id))
        self.assertNotIn('event_time',self.client.get(f"/v1/debug/runs/{run_id}/report").json()['findings'][0])

    def test_other_owner_and_production_are_hidden(self):
        run_id=self.submit()
        self.service.task_token="prod"
        self.assertEqual(404,self.client.get(f"/v1/debug/runs/{run_id}/report").status_code)

    def test_large_evidence_is_loaded_only_in_requested_page(self):
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        evidence=[{"evidence_id":f"e-{i}","file_id":"f-1" if i%2==0 else "f-2",
                   "text":"PRIVATE-LARGE-TEXT-"+str(i)+"x"*1000,"locator":{"page":i+1},
                   "source_role":"actual_record","content_hash":"hash-1" if i%2==0 else "hash-2"}
                  for i in range(1500)]
        snapshot={"files":[{"file_id":"f-1","original_name":"日报.pdf","content_hash":"hash-1"},
                           {"file_id":"f-2","original_name":"调度.pdf","content_hash":"hash-2"}],
                  "evidence":evidence,"source_complete":False,"coverage_gaps":[f"gap-{i}" for i in range(200)]}
        result={"status":"unverifiable","description":"无法判定","detail":"详情",
                "coverage_gaps":[f"gap-{i}" for i in range(200)],"findings":[],"propositions":[]}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=?,result_json=? WHERE run_id=?",
                       (json.dumps(snapshot,ensure_ascii=False),json.dumps(result,ensure_ascii=False),run_id))
        with patch.object(self.ledger,"get",side_effect=AssertionError("summary must not deserialize snapshot")):
            task=self.client.get("/v1/debug/tasks/"+run_id)
            report=self.client.get(f"/v1/debug/runs/{run_id}/report")
            self.assertNotIn("PRIVATE-LARGE-TEXT",task.text+report.text)
            self.assertLess(len(task.content),20000)
            self.assertLess(len(report.content),20000)
            self.assertEqual(1500,report.json()["counts"]["evidence"])
            page=self.client.get(f"/v1/debug/runs/{run_id}/evidence?limit=3&offset=2&file_id=f-1").json()
            self.assertEqual(750,page["total"])
            self.assertEqual(["e-4","e-6","e-8"],[item["evidence_id"] for item in page["items"]])
            self.assertTrue(page["has_more"])
            refs=self.client.get(f"/v1/debug/runs/{run_id}/evidence?refs=e-2,e-5&limit=1").json()
            self.assertEqual(2,refs["total"])
            files=self.client.get(f"/v1/debug/runs/{run_id}/files?limit=1").json()
            self.assertEqual(2,files["total"])
            self.assertEqual(750,files["items"][0]["evidence_count"])
            gaps=self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=gaps&limit=2&offset=198").json()
            self.assertEqual(["gap-198","gap-199"],gaps["items"])
            self.assertFalse(gaps["has_more"])

    def test_task_group_does_not_expose_production_or_real_callback_runs(self):
        spec=RunSpec('prod','secret-mine','stop_work_unload','2026-10',{'fixture':'clean'},
                     environment='production',callback_mode='none')
        run_id=self.ledger.accept(spec)['run_id']
        for suffix in ('report','files','evidence','diagnostics','progress'):
            self.assertEqual(404,self.client.get(f"/v1/debug/runs/{run_id}/{suffix}").status_code)
        self.assertEqual(404,self.client.get('/v1/debug/tasks/'+run_id).status_code)

    def test_failed_attachments_remain_in_file_scope(self):
        run_id=self.submit()
        acquisition={"rows":[{"attachment_id":"failed-a","attachment_name":"损坏.pdf","material_group":"prevention_daily",
                               "file_name_id":"catalog-1","attachment_url":"https://files.test/a.pdf","role":"actual_record"}]}
        snapshot={"files":[],"evidence":[],"acquisition":acquisition,
                  "attachment_acquisition":{"failures":[{"attachment_id":"failed-a","error":"TimeoutError"}]}}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=?,acquisition_json=? WHERE run_id=?",(json.dumps(snapshot),json.dumps(acquisition),run_id))
        files=self.client.get(f"/v1/debug/runs/{run_id}/files").json()
        self.assertEqual(1,files['total'])
        self.assertEqual('failed',files['items'][0]['status'])
        self.assertEqual('catalog-1',files['items'][0]['source_metadata']['file_name_id'])
        report=self.client.get(f"/v1/debug/runs/{run_id}/report").json()
        self.assertEqual(1,report['counts']['files'])
        self.assertEqual('TimeoutError',files['items'][0]['error'])

    def test_all_candidate_sources_and_truncated_report_findings_are_reachable(self):
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        findings=[{"code":f"finding-{i}","detail":"detail","evidence_refs":["ref"]} for i in range(123)]
        snapshot={"files":[],"evidence":[],"candidates":[{"quote":"native candidate"}],
                  "daily_candidate_evidence":[{"text":"visual candidate"}]}
        result={"status":"pending","description":"待补证","detail":"detail","findings":findings,
                "daily_issues":[{"description":f"day-{i}"} for i in range(122)]}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=?,result_json=? WHERE run_id=?",(json.dumps(snapshot),json.dumps(result),run_id))
        report=self.client.get(f"/v1/debug/runs/{run_id}/report").json()
        self.assertEqual(123,report['finding_count']); self.assertEqual(100,len(report['findings']))
        self.assertEqual(122,report['daily_issue_count']); self.assertEqual(100,len(report['daily_issues']))
        self.assertEqual(2,report['counts']['candidates'])
        candidates=self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=candidates&limit=1&offset=1").json()
        self.assertEqual(2,candidates['total']); self.assertEqual('visual candidate',candidates['items'][0]['text'])
        remaining=self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=findings&offset=100&limit=100").json()
        self.assertEqual(23,len(remaining['items']))
        days=self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=daily_issues&offset=100&limit=100").json()
        self.assertEqual(22,len(days['items']))
        snapshot['daily_candidate_evidence']=[]
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=? WHERE run_id=?",(json.dumps(snapshot),run_id))
        self.assertEqual('native candidate',self.client.get(f"/v1/debug/runs/{run_id}/diagnostics?section=candidates").json()['items'][0]['quote'])

    def test_mixed_owner_task_and_real_delivery_are_hidden(self):
        specs=[RunSpec('local','001','stop_work_unload','2026-10',{'fixture':'clean'},environment='test',callback_mode='none',task_key='mixed'),
               RunSpec('local','002','relief_effects','2026-10',{'fixture':'clean'},environment='test',callback_mode='none',task_key='mixed')]
        self.ledger.accept_task({'task_key':'mixed','raw':{},'callback_url':None},specs)
        self.assertEqual(404,self.client.get('/v1/debug/tasks/mixed').status_code)
        spec=RunSpec('local','001','stop_work_unload','2026-10',{'oceanbase':'real'},environment='test',callback_mode='real',
                     task_key='real',callback_url='https://callback.test/result')
        run_id=self.ledger.accept(spec)['run_id']
        self.assertEqual(404,self.client.get(f'/v1/debug/runs/{run_id}/report').status_code)

    def test_terminal_legacy_duration_uses_last_recorded_time(self):
        self.ledger.clock=lambda:1000
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        with self.ledger._tx() as db:
            db.execute("DELETE FROM run_progress WHERE run_id=? AND json_extract(event_json,'$.stage')='run' AND json_extract(event_json,'$.event')='finished'",(run_id,))
            db.execute("UPDATE run_progress SET created=1005 WHERE run_id=?",(run_id,))
        self.ledger.clock=lambda:5000
        task=self.client.get('/v1/debug/tasks/'+run_id).json()
        self.assertEqual(5000,task['elapsed_ms'])
        self.ledger.clock=lambda:6000
        self.assertEqual(5000,self.client.get('/v1/debug/tasks/'+run_id).json()['items'][0]['elapsed_ms'])

    def test_explicit_candidate_reference_resolves_provenance_without_becoming_registered_evidence(self):
        run_id=self.submit(); self.service.run_once(run_id=run_id)
        snapshot={"files":[{"file_id":"daily","original_name":"10月15日防冲日报.pdf","content_hash":"hash"}],
                  "evidence":[{"evidence_id":"actual","file_id":"daily","text":"原生文字","locator":{"page":1}}],
                  "candidates":[{"candidate_id":"extract-candidate","source_file_id":"daily","quote":"提取候选",
                                  "locator":{"row":2},"source_role":"actual_record"}],
                  "daily_candidate_evidence":[{"candidate_id":"candidate-12840","file_id":"daily","text":"危险等级强",
                      "locator":{"page":3,"provider":"vision"},"source_role":"actual_record","content_hash":"hash",
                      "metadata":{"recognition_quality":"candidate"}}]}
        with self.ledger._tx() as db:
            db.execute("UPDATE runs SET snapshot_json=? WHERE run_id=?",(json.dumps(snapshot),run_id))
        default=self.client.get(f'/v1/debug/runs/{run_id}/evidence').json()
        self.assertEqual(1,default['total']);self.assertFalse(default['items'][0]['is_candidate'])
        selected=self.client.get(f'/v1/debug/runs/{run_id}/evidence?refs=candidate-12840&file_id=daily').json()
        self.assertEqual(1,selected['total'])
        candidate=selected['items'][0]
        self.assertTrue(candidate['is_candidate']);self.assertEqual('candidate',candidate['quality'])
        self.assertEqual('危险等级强',candidate['text']);self.assertEqual({'page':3,'provider':'vision'},candidate['locator'])
        self.assertEqual('10月15日防冲日报.pdf',candidate['original_name'])
        mixed=self.client.get(f'/v1/debug/runs/{run_id}/evidence?refs=actual,candidate-12840,extract-candidate&limit=2').json()
        self.assertEqual(3,mixed['total']);self.assertTrue(mixed['has_more'])
        second=self.client.get(f'/v1/debug/runs/{run_id}/evidence?refs=actual,candidate-12840,extract-candidate&limit=2&offset=2').json()
        self.assertEqual('提取候选',second['items'][0]['text']);self.assertTrue(second['items'][0]['is_candidate'])
        self.assertEqual(0,self.client.get(f'/v1/debug/runs/{run_id}/evidence?refs=candidate-12840&file_id=other').json()['total'])
        self.assertEqual(1,self.client.get(f'/v1/debug/runs/{run_id}/evidence').json()['total'])


if __name__ == "__main__": unittest.main()
