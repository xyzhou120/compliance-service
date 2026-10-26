from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from compliance_service.api import create_app
from compliance_service.ledger import Ledger
from compliance_service.service import InspectionService


class FrontendHostingTests(unittest.TestCase):
    def test_built_frontend_same_origin_assets_legacy_and_production_boundary(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            web=root/'web';(web/'assets').mkdir(parents=True)
            (web/'index.html').write_text('<!doctype html><title>检查工作台</title><div id="app"></div>',encoding='utf-8')
            (web/'assets'/'app.js').write_text('export const ready=true',encoding='utf-8')
            service=InspectionService(Ledger(root/'test.sqlite3'))
            with patch('compliance_service.api.DEBUG_WEB_ROOT',web),TestClient(create_app(service)) as client:
                for route in ('/debug','/debug/'):
                    response=client.get(route)
                    self.assertEqual(200,response.status_code)
                    self.assertIn('检查工作台',response.text)
                    self.assertIn('text/html',response.headers['content-type'])
                self.assertIn('ready=true',client.get('/debug/assets/app.js').text)
                self.assertEqual(404,client.get('/debug/assets/missing.js').status_code)
                self.assertIn('模拟上层服务触发任务',client.get('/debug/legacy').text)
                service.task_token='production-only'
                self.assertEqual(404,client.get('/debug').status_code)
                self.assertEqual(404,client.get('/debug/assets/app.js').status_code)

    def test_source_checkout_without_build_keeps_legacy_entry_working(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            service=InspectionService(Ledger(root/'test.sqlite3'))
            with patch('compliance_service.api.DEBUG_WEB_ROOT',root/'missing-build'),TestClient(create_app(service)) as client:
                response=client.get('/debug')
                self.assertEqual(200,response.status_code)
                self.assertIn('模拟上层服务触发任务',response.text)


if __name__=='__main__': unittest.main()
