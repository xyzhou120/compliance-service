from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from PIL import Image

from compliance_service.contracts import RunPaused, RunSpec
from compliance_service.ocr_documents import parse_with_models, recognize_checked
from compliance_service.parsers import parse_document
from compliance_service.sources import LocalDocumentSource


def settings():
    return {'ocr': {'enabled': True, 'provider': 'http-json', 'url': 'https://test/ocr'},
            'vision': {'enabled': True, 'provider': 'openai-compatible',
                       'base_url': 'https://test/v1', 'model': 'test', 'max_output_tokens': 1024}}


class RecognitionRecoveryTests(unittest.TestCase):
    def test_transient_failure_retries_only_failed_provider_and_preserves_native_parse(self):
        calls = []
        vision_failed = [False]
        def transport(url, *_args):
            calls.append(url)
            if url.endswith('/ocr'):
                return {'text': '监测值8MPa'}
            if not vision_failed[0]:
                vision_failed[0] = True
                raise URLError('temporary connection failure')
            return {'choices': [{'message': {'content': '{"text":"监测值8MPa"}'}}]}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (16, 16), 'white').save(root / 'scan.png')
            with patch('compliance_service.ocr_documents.parse_document', wraps=parse_document) as native:
                first = parse_with_models(root / 'scan.png', file_id='scan', settings=settings(), transport=transport,
                                          cache_dir=root / 'cache')
                second = parse_with_models(root / 'scan.png', file_id='scan', settings=settings(), transport=transport,
                                           cache_dir=root / 'cache')
                self.assertEqual(1, native.call_count)
            self.assertEqual(1, sum(url.endswith('/ocr') for url in calls))
            self.assertEqual(2, sum(url.endswith('/chat/completions') for url in calls))
            self.assertEqual('incomplete', first['coverage'][0]['recognition']['status'])
            self.assertEqual('compared', second['coverage'][0]['recognition']['status'])
            self.assertEqual(first['content_hash'], second['content_hash'])

    def test_changed_token_budget_invalidates_only_vision_candidate(self):
        calls = []
        def transport(url, *_args):
            calls.append(url)
            return {'text': '8MPa'} if url.endswith('/ocr') else {'choices': [{'message': {'content': '{"text":"8MPa"}'}}]}
        with TemporaryDirectory() as directory:
            recognize_checked(b'image', 'image/png', settings(), transport=transport, cache_dir=directory)
            changed = settings()
            changed['vision']['max_output_tokens'] = 2048
            recognize_checked(b'image', 'image/png', changed, transport=transport, cache_dir=directory)
        self.assertEqual(1, sum(url.endswith('/ocr') for url in calls))
        self.assertEqual(2, sum(url.endswith('/chat/completions') for url in calls))

    def test_local_source_does_not_freeze_temporary_provider_failure(self):
        calls = []
        def transport(_transport, url, *_args):
            calls.append(url)
            if url.endswith('/ocr'):
                return {'text': '工作面W1'}
            if sum(value.endswith('/chat/completions') for value in calls) == 1:
                raise URLError('temporary')
            return {'choices': [{'message': {'content': '{"text":"工作面W1"}'}}]}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (16, 16), 'white').save(root / 'scan.png')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'source_system': 'test', 'mine_code': '001', 'environment': 'test',
                'period': {'start': '2026-10-01', 'end': '2026-10-31'}, 'source_complete': True,
                'files': [{'file_id': 'scan', 'path': 'scan.png', 'role': 'actual_record'}]}), encoding='utf-8')
            spec = RunSpec('test', '001', 'stop_work_unload', '2026-10',
                           {'manifest': str(manifest)}, environment='test', callback_mode='none')
            with patch('compliance_service.providers._transport_call', side_effect=transport):
                source = LocalDocumentSource(root / 'cache', model_settings=settings())
                first = source({'run_id': 'r1', 'spec': spec.to_dict()})
                second = source({'run_id': 'r2', 'spec': spec.to_dict()})
            self.assertEqual('incomplete', first['coverage'][0]['recognition']['status'])
            self.assertEqual('compared', second['coverage'][0]['recognition']['status'])
            self.assertEqual(1, sum(url.endswith('/ocr') for url in calls))
            self.assertEqual(2, sum(url.endswith('/chat/completions') for url in calls))

    def test_retry_retains_native_blocks_in_a_mixed_document(self):
        failed = [False]
        def transport(url, *_args):
            if url.endswith('/ocr'):
                return {'text': '扫描测值8MPa'}
            if not failed[0]:
                failed[0] = True
                raise URLError('temporary')
            return {'choices': [{'message': {'content': '{"text":"扫描测值8MPa"}'}}]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (16, 16), 'white').save(path)
            native = parse_document(path, file_id='scan')
            native['blocks'] = [{'block_id': 'native-title', 'text': '原生标题', 'kind': 'page', 'locator': {'page': 1}}]
            with patch('compliance_service.ocr_documents.parse_document', return_value=deepcopy(native)):
                first = parse_with_models(path, file_id='scan', settings=settings(), transport=transport, cache_dir=Path(directory) / 'cache')
                second = parse_with_models(path, file_id='scan', settings=settings(), transport=transport, cache_dir=Path(directory) / 'cache')
            self.assertEqual(native['blocks'][0], first['blocks'][0])
            self.assertEqual(native['blocks'][0], second['blocks'][0])
            self.assertEqual(2, len(second['blocks']))

    def test_pause_propagates_from_model_checkpoint(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (16, 16), 'white').save(path)
            def pause(_event):
                raise RunPaused()
            with self.assertRaises(RunPaused):
                parse_with_models(path, file_id='scan', settings=settings(), progress_callback=pause)


if __name__ == '__main__':
    unittest.main()
