"""Align only exact, hash-verified knowledge IDs; do not move whole folders."""
import argparse
import collections
import json
from pathlib import Path

from reconcile import API, KB, hashes, save, norm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--key-file', required=True)
    args = parser.parse_args()
    api = API(args.key_file)
    rows = json.loads((args.output / 'mapping.json').read_text())
    refs = collections.Counter(i for row in rows for i in row['weknora_ids'])
    candidates = [r for r in rows if r['folder_status'] == 'elsewhere' and r['artifact_provenance_verified']]
    before_path = args.output / 'before-folder-align.json'
    results_path = args.output / 'folder-align-results.json'
    before = json.loads(before_path.read_text()) if before_path.exists() else []
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    for row in candidates:
        for match in row['weknora_matches']:
            identity = match['id']
            if refs[identity] != 1:
                results.append({'id': identity, 'status': 'shared_reference_hold'})
                continue
            status, response = api.request('/knowledge/' + identity)
            doc = response.get('data') or {}
            if status != 200 or doc.get('knowledge_base_id') != KB or doc.get('file_hash') != row['markdown']['md5']:
                raise ValueError('remote_document_changed')
            if hashes(Path(row['source']['path']))['sha256'] != row['source']['sha256']:
                raise ValueError('source_changed')
            old_folder = norm(doc.get('folder_path') or '')
            if old_folder == row['expected_folder']:
                results.append({'id': identity, 'status': 'already_aligned'})
                continue
            if old_folder != norm(match.get('folder_path') or ''):
                raise ValueError('folder_changed_by_another_writer')
            before.append({k: doc.get(k) for k in ('id', 'file_hash', 'file_name', 'folder_path', 'parse_status', 'enable_status')})
            save(args.output / 'before-folder-align.json', before)
            intent = {'id': identity, 'from': old_folder, 'to': row['expected_folder'], 'status': 'move_intent'}
            results.append(intent)
            save(args.output / 'folder-align-results.json', results)
            status, response = api.request('/knowledge/folder',
                json.dumps({'kb_id': KB, 'knowledge_ids': [identity], 'folder_path': row['expected_folder']}).encode(),
                {'Content-Type': 'application/json'})
            if status != 200 or not response.get('success'):
                intent.update(status='move_rejected', http_status=status)
                save(args.output / 'folder-align-results.json', results)
                continue
            status, response = api.request('/knowledge/' + identity)
            if status != 200 or norm(response['data'].get('folder_path') or '') != row['expected_folder']:
                raise ValueError('folder_move_not_verified')
            intent['status'] = 'aligned_verified'
            save(args.output / 'folder-align-results.json', results)
    print(json.dumps(dict(collections.Counter(r['status'] for r in results)), ensure_ascii=False))


if __name__ == '__main__':
    main()
