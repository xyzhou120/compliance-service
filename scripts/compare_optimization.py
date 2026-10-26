"""Compare identical real bytes, coverage and evidence across implementation stages."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation', type=Path, default=Path('.'))
    parser.add_argument('--baseline', type=Path, default=Path('.local/optimization-baseline-20261026/real-files.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--staged', action='store_true', help='apply OceanBase stop-item material priority to the frozen manifest')
    args = parser.parse_args()
    sys.path.insert(0, str(args.implementation.resolve()))
    from compliance_service.ledger import Ledger
    from compliance_service.service import InspectionService
    from compliance_service.sources import LocalDocumentSource
    from compliance_service.contracts import canonical, digest
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    for f in baseline['files']:
        if hashlib.sha256(Path(f['path']).read_bytes()).hexdigest() != f['sha256']:
            raise RuntimeError('Frozen comparison input changed: ' + f['file_id'])
    manifest_path = Path(baseline['manifest'])
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    args.output.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(args.output/'ledger.sqlite3')
    source = LocalDocumentSource(args.output/'artifacts', model_settings={'ocr':{'enabled':False},'vision':{'enabled':False}})
    def read(run):
        selected = {**run, 'catalog_profile': 'oceanbase'} if args.staged else run
        return source(selected, progress_callback=lambda event: ledger.record_progress(run['run_id'],run['worker_id'],run['fence'],event))
    service = InspectionService(ledger, source=read)
    rounds=[]
    for name in ('cold','warm'):
        item=service.submit({'sourceSystem':manifest['source_system'],'mineCode':manifest['mine_code'],
            'environment':manifest['environment'],'checkCycle':'2026-10','scope':{'manifest':str(manifest_path)},
            'callbackMode':'none','items':[{'itemCode':'stop_work_unload'}]})['items'][0]
        start=perf_counter(); service.run_once(run_id=item['run_id']); elapsed=perf_counter()-start
        run=ledger.get(item['run_id'],manifest['source_system'],manifest['mine_code'],manifest['environment'])
        snapshot=run['snapshot'] or {}; result=run['result'] or {}
        progress=ledger.get_progress(item['run_id'],manifest['source_system'],manifest['mine_code'],manifest['environment'],limit=500)['events']
        evidence=sorted((e['file_id'],e['content_hash'],canonical(e['locator']),hashlib.sha256(e['text'].encode()).hexdigest()) for e in snapshot.get('evidence',[]))
        coverage=sorted((x['file_id'],x['unit_id'],x['state'],x.get('reason','')) for x in snapshot.get('coverage',[]))
        rounds.append({'round':name,'seconds':round(elapsed,3),'status':result.get('status'),
            'files':len(snapshot.get('files',[])),'file_hashes':{f['file_id']:f['content_hash'] for f in snapshot.get('files',[])},
            'evidence_count':len(evidence),'evidence_locations_and_text_digest':digest(evidence),
            'coverage_count':len(coverage),'coverage_digest':digest(coverage),
            'coverage_states':dict(Counter(x[2] for x in coverage)),
            'file_processing_order':[e['file_id'] for e in progress if e.get('event')=='file_started'],
            'provisional_scan':next(({'files':e.get('file_total'),'evidence':e.get('evidence_count'),
                                     'facts':e.get('fact_count'),'candidates':e.get('candidate_count')}
                                    for e in progress if e.get('stage')=='screening'),None),
            'gap_count':len(result.get('coverage_gaps',[])), 'cache':snapshot.get('cache',{}),
            'evidence_refs_valid':set(result.get('evidence_refs',[])) <= {e['evidence_id'] for e in snapshot.get('evidence',[])}})
    report={'profile':'same-real-bytes-native-only-no-model-no-callback','recognition_included':False,
        'implementation':str(args.implementation.resolve()),'input_count':len(baseline['files']),'rounds':rounds}
    (args.output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**report,'rounds':[{k:v for k,v in row.items() if k!='file_hashes'} for row in rounds]},ensure_ascii=False))


if __name__=='__main__': main()
