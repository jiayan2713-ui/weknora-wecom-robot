"""Read-only three-way inventory. No source writes, remote deletes or moves."""
import argparse
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SOURCE = Path('/Users/xinchengyuan/Library/Containers/com.tencent.WeWorkMac/Data/WeDrive/海那边/项目to咨询')
MARKDOWN = Path('/Users/xinchengyuan/Desktop/当前解析结果/项目to咨询/Markdown')
PROJECT = Path(__file__).resolve().parents[2]
KB = '98f02397-d24e-45e1-a7cd-d15ee586a0bd'


def norm(value):
    return unicodedata.normalize('NFC', str(value))


def hashes(path):
    before = path.stat()
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
            md5.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('file_changed_during_read')
    return {'sha256': sha.hexdigest(), 'md5': md5.hexdigest(), 'size': after.st_size,
            'mtime_ns': after.st_mtime_ns}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    import sys
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    from docflow.common import write_json
    write_json(path.resolve(), data)



class API:
    def __init__(self, key_path, base_url='http://127.0.0.1:8090', kb_id=KB):
        self.key = Path(key_path).read_text().strip()
        self.auth = {'Authorization': 'Bearer ' + self.key} if self.key.count('.') == 2 else {'X-API-Key': self.key}
        self.base = base_url.rstrip('/') + '/api/v1'
        self.kb_id = kb_id
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, route, data=None, headers=None, method=None):
        request = urllib.request.Request(self.base + route, data=data,
            headers={**self.auth, **(headers or {})}, method=method)
        try:
            with self.opener.open(request, timeout=90) as response:
                body = response.read()
                return response.status, json.loads(body) if body else {'success': True}
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def inventory(self):
        documents, seen, total = [], set(), None
        page = 1
        while True:
            query = urllib.parse.urlencode({'page': page, 'page_size': 100,
                                           'sort_by': 'file_name', 'sort_order': 'asc'})
            status, result = self.request(f'/knowledge-bases/{self.kb_id}/knowledge?{query}')
            if status != 200 or not result.get('success'):
                raise RuntimeError(f'inventory_http_{status}')
            if total is None:
                total = result['total']
            if result['total'] != total:
                raise RuntimeError('remote_inventory_changed_retry_required')
            batch = result['data']
            for doc in batch:
                if doc['id'] in seen:
                    raise RuntimeError('remote_pagination_duplicate')
                seen.add(doc['id'])
                documents.append({k: doc.get(k) for k in (
                    'id', 'knowledge_base_id', 'title', 'file_name', 'file_type', 'file_hash',
                    'file_size', 'folder_path', 'parse_status', 'enable_status',
                    'error_message', 'summary_status', 'pending_subtasks_count', 'updated_at')})
            if len(documents) == total:
                return documents
            if not batch or len(documents) > total:
                raise RuntimeError('remote_inventory_incomplete')
            page += 1


def scan(root, markdown_only=False):
    rows = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name == '.DS_Store':
            continue
        relative = norm(path.relative_to(root))
        if any(part.startswith('.') for part in Path(relative).parts):
            continue
        if path.is_symlink():
            rows.append({'relative': relative, 'status': 'symlink_hold'})
            continue
        if path.suffix == '.icloud':
            rows.append({'relative': relative, 'status': 'placeholder_hold'})
            continue
        if markdown_only and path.suffix.lower() != '.md':
            continue
        try:
            rows.append({'relative': relative, 'path': str(path), 'status': 'readable', **hashes(path)})
        except (OSError, ValueError) as error:
            rows.append({'relative': relative, 'status': 'read_error', 'error': type(error).__name__})
        if len(rows) % 500 == 0:
            print(f'scan {root.name}: {len(rows)}', flush=True)
    return rows


def reconcile(source_rows, markdown_rows, documents, state, marker, recovered=None):
    sources = {r['relative']: r for r in source_rows}
    markdowns = {r['relative']: r for r in markdown_rows if r['status'] == 'readable'}
    records = state['records']
    path_records = collections.defaultdict(list)
    for rid, record in records.items():
        for relative in record.get('paths', []):
            path_records[norm(relative)].append((rid, record))
    artifacts = {norm(k.removeprefix('项目to咨询/Markdown/')): v
                 for k, v in marker['files'].items() if k.startswith('项目to咨询/Markdown/')}
    supplements = {norm(r['source_relative'] + '.md'): r for r in (recovered or []) if r['status'] == 'recovered'}
    by_hash = collections.defaultdict(list)
    for doc in documents:
        if doc.get('file_type', '').lower() in ('md', 'markdown'):
            by_hash[doc.get('file_hash')].append(doc)
    mappings = []
    for relative, md in markdowns.items():
        src_relative = relative[:-3]
        src = sources.get(src_relative)
        candidates = path_records.get(src_relative, [])
        artifact = artifacts.get(relative)
        rid = artifact.get('record_id') if artifact else None
        record = records.get(rid) if rid else None
        if record is None and len(candidates) == 1:
            rid, record = candidates[0]
        expected = record.get('active', {}).get('hash') or record.get('hash') if record else None
        marker_match = bool(artifact and artifact.get('sha256') == md.get('sha256'))
        current = bool(src and src.get('sha256') and expected == src['sha256'] and marker_match)
        supplement = supplements.get(relative)
        if supplement and src and supplement['source_sha256'] == src.get('sha256') and supplement['markdown_sha256'] == md.get('sha256'):
            current = True
            expected = supplement['source_sha256']
            rid = supplement['record_id']
        matches = [d for d in by_hash.get(md.get('md5'), []) if d.get('file_size') == md.get('size')]
        expected_folder = norm('项目to咨询/' + str(Path(relative).parent)).rstrip('/.')
        target_matches = [d for d in matches if norm(d.get('folder_path') or '').strip('/') == expected_folder]
        chosen = target_matches or matches
        mapping = {'source_relative': src_relative, 'source': src, 'markdown': md,
                   'record_id': rid, 'expected_source_sha256': expected,
                   'artifact_provenance_verified': current,
                   'marker_content_match': marker_match,
                   'expected_folder': expected_folder, 'weknora_matches': matches,
                   'weknora_ids': [d['id'] for d in chosen],
                   'folder_status': 'matched' if target_matches else 'elsewhere' if matches else 'missing',
                   'index_status': 'completed' if chosen and all(d.get('parse_status') == 'completed' for d in chosen) else 'not_ready' if chosen else 'missing',
                   'action': 'mapped' if matches else 'upload_missing' if current else 'provenance_hold'}
        mapping['content_status'] = supplement.get('content_status', 'not_separately_assessed') if supplement else 'not_separately_assessed'
        if mapping['content_status'] in ('no_text_hold', 'body_missing_hold'):
            mapping['action'] = 'transcription_hold'
        if not current:
            mapping['source_status'] = ('source_missing_hold' if not src else
                'source_changed_hold' if expected and src.get('sha256') != expected else 'provenance_hold')
        else:
            mapping['source_status'] = 'verified_current'
        mappings.append(mapping)
    represented_ids = {d['id'] for row in mappings if row['folder_status'] == 'matched' for d in row['weknora_matches']}
    for row in mappings:
        if row['folder_status'] == 'elsewhere' and any(i in represented_ids for i in row['weknora_ids']):
            row['folder_status'] = 'shared_content_alias'
    unmapped_sources = [r for r in source_rows if r['relative'] + '.md' not in markdowns]
    return mappings, unmapped_sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--key-file', default=str(PROJECT / 'outputs/15文件10引擎解析对比_20260928/weknora-api-key.txt'))
    parser.add_argument('--reuse-local', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.reuse_local:
        sources = json.loads((args.output / 'sources.json').read_text())
        sources = [r for r in sources if not any(p.startswith('.') for p in Path(r['relative']).parts)]
        markdowns = scan(MARKDOWN, True)
        save(args.output / 'markdowns.json', markdowns)
    else:
        sources = scan(SOURCE)
        save(args.output / 'sources.json', sources)
        markdowns = scan(MARKDOWN, True)
        save(args.output / 'markdowns.json', markdowns)
    api = API(args.key_file)
    docs = api.inventory()
    save(args.output / 'weknora.json', docs)
    state = json.loads((PROJECT / '.local/wecom-sync/project-consultation-local-20260929/live/state.json').read_text())
    marker = json.loads((PROJECT / 'outputs/当前解析结果/.managed-results.json').read_text())
    save(args.output / 'provenance-marker.json', marker)
    supplement_path = args.output / 'recovered-markdown.json'
    recovered = json.loads(supplement_path.read_text()) if supplement_path.exists() else []
    mappings, missing = reconcile(sources, markdowns, docs, state, marker, recovered)
    save(args.output / 'mapping.json', mappings)
    save(args.output / 'sources-without-markdown.json', missing)
    summary = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'source_paths': len(sources),
               'source_status': dict(collections.Counter(r['status'] for r in sources)),
               'markdown_paths': len(markdowns), 'kb_documents': len(docs),
               'actions': dict(collections.Counter(r['action'] for r in mappings)),
               'source_versions': dict(collections.Counter(r['source_status'] for r in mappings)),
               'folders': dict(collections.Counter(r['folder_status'] for r in mappings)),
               'index_states': dict(collections.Counter(r['index_status'] for r in mappings)),
               'source_paths_without_markdown': len(missing), 'remote_mutations': False}
    save(args.output / 'summary.json', summary)
    with (args.output / 'mapping.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['原文件相对路径', 'Markdown路径', '来源版本', 'WeKnora文档ID', '目标目录', '实际目录', '索引状态', '动作'])
        for row in mappings:
            writer.writerow([row['source_relative'], row['markdown']['path'], row['source_status'],
                             ';'.join(row['weknora_ids']), row['expected_folder'],
                             ';'.join(str(d.get('folder_path') or '') for d in row['weknora_matches']), row['index_status'], row['action']])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
