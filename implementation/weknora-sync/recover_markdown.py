"""Reuse completed parser artifacts; never alter original sources or review drafts."""
import json
from pathlib import Path
import shutil

from reconcile import PROJECT, SOURCE, MARKDOWN, hashes, save

ROOT = PROJECT / 'outputs/机器人5三方对账_20261010'


def main():
    candidates = json.loads((ROOT / 'recovery-candidates.json').read_text())
    mapping = json.loads((ROOT / 'mapping.json').read_text())
    marker = json.loads((PROJECT / 'outputs/当前解析结果/.managed-results.json').read_text())
    for row in mapping:
        if row['source_status'] == 'verified_current' or not row.get('source'):
            continue
        entry = marker['files'].get('项目to咨询/Markdown/' + row['markdown']['relative'])
        if not entry:
            continue
        pub = Path(entry['source'])
        trace = pub.parent / 'source-trace.json'
        if not trace.exists():
            continue
        provenance = json.loads(trace.read_text()).get('parser_provenance', {})
        if provenance.get('source_hash') != row['source']['sha256']:
            continue
        candidates.append({'source_relative': row['source_relative'],
            'source_sha256': row['source']['sha256'], 'record_id': row['record_id'],
            'publication': str(pub), 'source_trace': str(trace),
            'markdown_sha256': hashes(pub)['sha256'], 'raw_verified': True,
            'has_images': '![' in pub.read_text(), 'replaces_stale_copy': True})
    results = []
    for item in candidates:
        if not item['raw_verified'] or item['has_images']:
            results.append(dict(item, status='content_assets_hold'))
            continue
        relative = Path(item['source_relative'] + '.md')
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe_relative')
        source = SOURCE / item['source_relative']
        pub = Path(item['publication'])
        if hashes(source)['sha256'] != item['source_sha256'] or hashes(pub)['sha256'] != item['markdown_sha256']:
            raise ValueError('recovery_source_changed')
        target = MARKDOWN / relative
        if target.exists() and hashes(target)['sha256'] != item['markdown_sha256']:
            backup = ROOT / 'before-markdown' / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or hashes(target)['sha256'] != item['markdown_sha256']:
            shutil.copy2(pub, target)
        if hashes(target)['sha256'] != item['markdown_sha256']:
            raise ValueError('recovered_copy_mismatch')
        results.append(dict(item, status='recovered', destination=str(target)))
    save(ROOT / 'recovered-markdown.json', results)
    print('Recovered Markdown:', sum(r['status'] == 'recovered' for r in results))
    # Folder upload into the KB root preserves project-relative paths.
    package = ROOT / '补齐上传包/项目to咨询'
    for row in mapping:
        if row['action'] != 'upload_missing':
            continue
        target = package / row['markdown']['relative']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(row['markdown']['path'], target)
    for item in results:
        if item['status'] != 'recovered':
            continue
        target = package / (item['source_relative'] + '.md')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item['destination'], target)
    print('Staged upload paths:', len(list(package.rglob('*.md'))))


if __name__ == '__main__':
    main()
