"""Explicit, audited parser upgrades without overwriting historical checkpoints."""
from pathlib import Path
import uuid

from .common import WorkflowError, artifact, digest, fingerprint, now, read_json, stable_copy, verify, write_json
from .parser import cache_identity, verify_cache


def upgrade_document(store, document, config, prompt, request_id):
    verify(store.root, document['source'])
    target_key = cache_identity(document, config, prompt)
    previous_key = document.get('cache_key')
    if not previous_key or previous_key == target_key:
        return
    if document.get('parser_upgrade_ref'):
        previous_upgrade = read_json(verify(store.root, document['parser_upgrade_ref']))
        if previous_upgrade.get('target_key') == target_key and previous_upgrade.get('previous_key') == previous_key:
            return
    old = store.path(f"parse-cache/{previous_key}/{document['cache_version']}")
    record = {'schema_version': 1, 'request_id': request_id, 'created_at': now(),
              'source_hash': document['source']['sha256'], 'previous_key': previous_key,
              'previous_version': document['cache_version'], 'target_key': target_key,
              'prompt_hash': fingerprint(prompt), 'previous_prepared_ref': document.get('prepared_ref'),
              'previous_unit_refs': document.get('unit_refs', {}), 'previous_cache_ref': document.get('cache_ref')}
    saved = None
    if document.get('prepared_ref'):
        saved = read_json(verify(store.root, document['prepared_ref']))
        for ref in saved['artifacts']:
            verify(store.root, ref)
        units = {u['id']: u for u in saved['units']}
        for unit_id, ref in document.get('unit_refs', {}).items():
            result = read_json(verify(store.root, ref))
            if unit_id not in units or result.get('input_hash') != fingerprint(units[unit_id]):
                raise WorkflowError('INTEGRITY_ERROR', '旧单元与准备输入不一致，禁止迁移')
    elif document.get('unit_refs'):
        raise WorkflowError('INTEGRITY_ERROR', '旧单元缺少准备索引，禁止迁移')
    if document.get('cache_ref'):
        manifest = verify_cache(store, document['cache_ref'])
        if (manifest['source_hash'] != record['source_hash'] or manifest['cache_key'] != previous_key
                or manifest['cache_version'] != document['cache_version']):
            raise WorkflowError('INTEGRITY_ERROR', '旧缓存身份不一致，禁止迁移')
        record['action'] = 'retain_complete'
        path = store.path(f"parser-upgrades/{request_id}/{document['id']}.json")
        write_json(path, record, replace=False)
        document['parser_upgrade_ref'] = artifact(store.root, path)
        return
    version = uuid.uuid4().hex
    new = store.path(f'parse-cache/{target_key}/{version}')
    new.mkdir(parents=True, exist_ok=True)
    if saved:
        # Snapshot partial responses as well, preserving paid-call and retry budgets.
        for path in old.rglob('*'):
            if path.is_symlink():
                raise WorkflowError('UNSAFE_PATH', '旧缓存包含符号链接')
            if path.is_file() and path.name not in {'parse-manifest.json', 'prepared-index.json'}:
                stable_copy(path, new / path.relative_to(old))
        def remap(ref):
            source = verify(store.root, ref)
            target = new / source.relative_to(old)
            return artifact(store.root, target)
        for call_path in new.glob('u*/**/call.json'):
            call = read_json(call_path)
            if call.get('response_sha256'):
                response = call_path.with_name('response.json')
                if not response.is_file() or digest(response) != call['response_sha256']:
                    raise WorkflowError('INTEGRITY_ERROR', '旧模型响应指纹不一致，禁止迁移')
        prepared_index = new / 'prepared-index.json'
        write_json(prepared_index, {'units': saved['units'], 'artifacts': [remap(r) for r in saved['artifacts']]}, replace=False)
        prepared_ref = artifact(store.root, prepared_index)
        unit_refs = {k: remap(v) for k, v in document.get('unit_refs', {}).items()}
    record.update(action='fork_incomplete', target_version=version)
    path = store.path(f"parser-upgrades/{request_id}/{document['id']}.json")
    write_json(path, record, replace=False)
    document.update(cache_key=target_key, cache_version=version, state='pending', parser_upgrade_ref=artifact(store.root, path))
    if saved:
        document.update(prepared_ref=prepared_ref, unit_refs=unit_refs)
