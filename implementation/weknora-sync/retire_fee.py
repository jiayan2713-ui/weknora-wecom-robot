"""Retire the one explicitly requested superseded fee document after guards."""
import hashlib
import json
from pathlib import Path
import time

from reconcile import API, KB, PROJECT, hashes, save


def main():
    root = PROJECT / 'outputs/机器人5三方对账_20261010'
    folder = root / '旧版费用表退役准备'
    plan = json.loads((folder / '退役计划.json').read_text())
    api = API(PROJECT / '.local/weknora-sync/api-key.txt')
    backup = folder / '旧版原始Markdown.md'
    assert hashes(backup)['sha256'] == plan['backup_sha256']
    assert hashes(backup)['md5'] == plan['old_file_md5']
    mappings = json.loads((root / 'mapping.json').read_text())
    assert not any(plan['old_id'] in m['weknora_ids'] for m in mappings)
    row = next(m for m in mappings if plan['new_id'] in m['weknora_ids'])
    assert hashes(Path(row['source']['path']))['sha256'] == row['source']['sha256']
    assert hashes(Path(row['markdown']['path']))['sha256'] == row['markdown']['sha256']
    status, response = api.request('/knowledge/' + plan['new_id'])
    new = response.get('data') or {}
    assert status == 200 and new['knowledge_base_id'] == KB
    assert new['parse_status'] == 'completed' and new['enable_status'] == 'enabled'
    assert new['file_hash'] == row['markdown']['md5']
    verification = json.loads((root / 'upload-retrieval-verification.json').read_text())
    assert any(v['id'] == plan['new_id'] and v['retrieval_verified'] for v in verification)
    status, response = api.request('/knowledge/' + plan['old_id'])
    if status == 404:
        plan.update(executed=True, status='already_absent')
        save(folder / '退役执行结果.json', plan)
        print('Old fee entry already absent; newest version verified')
        return
    old = response['data']
    assert status == 200 and old['knowledge_base_id'] == KB
    assert old['file_hash'] == plan['old_file_md5'] and old['file_name'] == plan['file_name']
    assert old['folder_path'] == row['expected_folder']
    plan['status'] = 'delete_intent'
    save(folder / '退役执行结果.json', plan)
    status, response = api.request('/knowledge/' + plan['old_id'], method='DELETE')
    if status not in (200, 202, 204):
        raise ValueError('old_fee_retirement_rejected')
    for _ in range(30):
        status, response = api.request('/knowledge/' + plan['old_id'])
        if status == 404:
            plan.update(executed=True, status='removed_verified', newest_version_preserved=True)
            save(folder / '退役执行结果.json', plan)
            print('One superseded fee entry removed and absence verified; newest version preserved')
            return
        time.sleep(1)
    raise ValueError('old_fee_retirement_not_confirmed')


if __name__ == '__main__':
    main()
