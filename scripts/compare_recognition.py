"""Bounded real-page OCR/vision comparison with no callback or business verdict."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation', type=Path, default=Path('.'))
    parser.add_argument('--baseline', type=Path, default=Path('.local/optimization-baseline-20261026/real-files.json'))
    parser.add_argument('--file-id', default='design-0001')
    parser.add_argument('--concurrency', type=int, choices=(1,2), default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.implementation.resolve()))
    from compliance_service.config import load_model_settings
    from compliance_service.ocr_documents import parse_with_models
    b = json.loads(args.baseline.read_text(encoding='utf-8'))
    entry = next(f for f in b['files'] if f['file_id'] == args.file_id)
    path = Path(entry['path'])
    if sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise RuntimeError('Frozen input has changed')
    settings = load_model_settings()
    if not all(settings[k]['enabled'] for k in ('ocr', 'vision')):
        raise RuntimeError('Both configured providers are required for this comparison')
    settings['ocr']['max_pages'] = 2
    settings['ocr']['recognition_concurrency'] = args.concurrency
    started = perf_counter()
    parsed = parse_with_models(path, file_id=entry['file_id'], settings=settings)
    elapsed = perf_counter() - started
    eligible = [u for u in parsed['coverage'] if u.get('recognition')]
    report = {'implementation':str(args.implementation.resolve()),'input_file_id':entry['file_id'],
              'input_sha256':entry['sha256'],'elapsed_seconds':round(elapsed,2),
              'page_concurrency':args.concurrency,
              'providers_enabled':2,'recognized_pages':len(eligible),
              'recognition_states':dict(Counter(u['recognition']['status'] for u in eligible)),
              'page_locators':[u['unit_id'] for u in eligible],
              'issues':dict(Counter(issue for u in eligible for issue in u['recognition'].get('issues',[]))),
              'candidate_count':sum(len(img.get('recognition_candidates',[])) for u in eligible
                                    for img in u['recognition'].get('images',[])),
              'recognized_block_count':sum(block.get('kind')=='recognized_page' for block in parsed['blocks'])}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
