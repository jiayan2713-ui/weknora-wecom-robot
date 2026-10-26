"""Publish only provenance-verified missing Markdown; retain every existing item."""
import argparse
import collections
import json
from pathlib import Path
import uuid
import mimetypes

from reconcile import API, KB, PROJECT, hashes, save


def multipart(path, relative, metadata):
    metadata = dict(metadata)
    process = metadata.pop('_process_config', None)
    boundary = 'weknora-sync-' + uuid.uuid4().hex
    parts = []
    for name, value in [('fileName', relative), ('metadata', json.dumps(metadata, ensure_ascii=False))]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    filename = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
    mime = 'text/markdown' if path.suffix.lower() == '.md' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
    parts.append(path.read_bytes())
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    if process:
        # Insert the per-file parser choice as a form field, outside business metadata.
        extra = f'--{boundary}\r\nContent-Disposition: form-data; name="process_config"\r\n\r\n'.encode() + json.dumps(process).encode() + b'\r\n'
        parts.insert(0, extra)
    return b''.join(parts), {'Content-Type': 'multipart/form-data; boundary=' + boundary}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key-file', default=str(PROJECT / 'outputs/15文件10引擎解析对比_20260928/weknora-api-key.txt'))
    parser.add_argument('--retry-rejected', action='store_true')
    args = parser.parse_args()
    mappings = json.loads((args.output / 'mapping.json').read_text())
    api = API(args.key_file)
    log_path = args.output / 'publish-attempts.json'
    attempts = json.loads(log_path.read_text()) if log_path.exists() else {}
    groups = collections.defaultdict(list)
    for row in mappings:
        if row['action'] == 'upload_missing':
            groups[row['markdown']['sha256']].append(row)
    for fingerprint, rows in groups.items():
        previous = attempts.get(fingerprint)
        if previous and not (args.retry_rejected and previous['status'] == 'rejected'):
            continue  # Never repeat an unknown prior write.
        row = rows[0]
        path = Path(row['markdown']['path'])
        if hashes(path)['sha256'] != fingerprint:
            attempts[fingerprint] = {'status': 'markdown_changed_hold'}
            save(log_path, attempts)
            continue
        source_hash = hashes(Path(row['source']['path']))['sha256']
        if source_hash != row['source']['sha256']:
            attempts[fingerprint] = {'status': 'source_changed_hold'}
            save(log_path, attempts)
            continue
        relative = '项目to咨询/' + row['markdown']['relative']
        metadata = {'sync_scope': 'project-consultation', 'sync_source_relative': row['source_relative'],
                    'sync_source_sha256': source_hash, 'sync_artifact_sha256': fingerprint}
        payload, headers = multipart(path, relative, metadata)
        # Freeze exactly what is transmitted, not merely the earlier disk read.
        if hashes(path)['sha256'] != fingerprint:
            raise RuntimeError('markdown_changed_during_multipart')
        attempts[fingerprint] = {'status': 'upload_intent', 'path': str(path),
                                 'relative': relative, 'source_aliases': [r['source_relative'] for r in rows]}
        if previous:
            attempts[fingerprint]['previous_attempt'] = previous
        save(log_path, attempts)
        try:
            status, result = api.request(f'/knowledge-bases/{KB}/knowledge/file', payload, headers)
            doc = result.get('data') or {}
            if status in (200, 201, 409) and doc.get('id'):
                if doc.get('file_hash') != row['markdown']['md5'] or doc.get('file_size') != row['markdown']['size']:
                    raise RuntimeError('returned_document_fingerprint_mismatch')
                attempts[fingerprint].update(status='duplicate_linked' if status == 409 else 'uploaded',
                    remote_id=doc['id'], folder_path=doc.get('folder_path'), parse_status=doc.get('parse_status'))
            else:
                attempts[fingerprint].update(status='rejected', http_status=status,
                                             code=result.get('code'), message=result.get('message'))
        except Exception as error:
            attempts[fingerprint].update(status='result_uncertain', error=type(error).__name__)
        save(log_path, attempts)
        if len(attempts) % 20 == 0:
            print('publish', len(attempts), dict(collections.Counter(a['status'] for a in attempts.values())), flush=True)
    print(json.dumps(dict(collections.Counter(a['status'] for a in attempts.values())), ensure_ascii=False))


if __name__ == '__main__':
    main()
