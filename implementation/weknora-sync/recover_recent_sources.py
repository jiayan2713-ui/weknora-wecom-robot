"""Adopt already parsed new sources without another model call."""
import json
from pathlib import Path
import shutil

from reconcile import PROJECT, SOURCE, MARKDOWN, hashes, save

ROOT = PROJECT / 'outputs/机器人5三方对账_20261010'


def main():
    fresh = json.loads((ROOT / 'source-freshness-check.json').read_text())
    sources = {r['relative']: r for r in json.loads((ROOT / 'sources.json').read_text())}
    state = json.loads((PROJECT / '.local/wecom-sync/project-consultation-local-20260929/live/state.json').read_text())
    by_path = {p: (rid, rec) for rid, rec in state['records'].items() for p in rec.get('paths', [])}
    marker = json.loads((PROJECT / 'outputs/当前解析结果/.managed-results.json').read_text())
    recovered = json.loads((ROOT / 'recovered-markdown.json').read_text())
    results = []
    for relative in fresh['new']:
        rid, rec = by_path.get(relative, (None, {}))
        candidates = []
        entry = marker['files'].get('项目to咨询/Markdown/' + relative + '.md')
        if entry:
            candidates.append(Path(entry['source']))
        key = rec.get('pending', {}).get('key')
        if key:
            candidates += list((PROJECT / '.local/wecom-sync/project-consultation-local-20260929/live/jobs' / key).rglob('publication.md'))
        selected = None
        for pub in candidates:
            trace = pub.parent / 'source-trace.json'
            if not trace.exists():
                continue
            provenance = json.loads(trace.read_text()).get('parser_provenance', {})
            if provenance.get('source_hash') != sources[relative]['sha256'] or '![' in pub.read_text():
                continue
            selected = pub
            break
        if not selected:
            results.append({'source_relative': relative, 'status': 'no_complete_artifact_hold'})
            continue
        target = MARKDOWN / (relative + '.md')
        if hashes(SOURCE / relative)['sha256'] != sources[relative]['sha256']:
            raise ValueError('source_changed')
        fingerprint = hashes(selected)['sha256']
        if target.exists() and hashes(target)['sha256'] != fingerprint:
            raise ValueError('existing_markdown_conflict')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(selected, target)
        item = {'source_relative': relative, 'source_sha256': sources[relative]['sha256'],
            'record_id': rid, 'status': 'recovered', 'markdown_sha256': fingerprint,
            'destination': str(target), 'source_trace': str(selected.parent / 'source-trace.json'),
            'publication': str(selected), 'model_called_this_round': False, 'human_review': False}
        recovered = [r for r in recovered if r['source_relative'] != relative] + [item]
        results.append(item)
        stage = ROOT / '补齐上传包/项目to咨询' / (relative + '.md')
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, stage)
    save(ROOT / 'recovered-markdown.json', recovered)
    save(ROOT / 'recent-source-recovery.json', results)
    print('Recent sources recovered:', sum(r['status'] == 'recovered' for r in results), '/', len(results))


if __name__ == '__main__':
    main()
