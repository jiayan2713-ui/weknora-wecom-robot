"""Live isolated deletion smoke; never writes to the WeDrive source directory."""
import json
from pathlib import Path
import time

from reconcile import API, PROJECT, save
from worker import Sync


def main():
    output = PROJECT / 'outputs' / ('机器人5自动删除验收_' + time.strftime('%Y%m%d_%H%M%S'))
    source = output / 'fixture-source'
    source.mkdir(parents=True)
    baseline = output / 'empty-baseline.json'
    save(baseline, [])
    cfg = json.loads((PROJECT / '.local/weknora-sync/config.json').read_text())
    production_grace = cfg.get('delete_grace_seconds', 900)
    cfg.update(source_root=str(source), markdown_root=str(output / 'fixture-markdown'),
        state_dir=str(output / 'state'), baseline_mapping=str(baseline),
        folder_root=output.name, stable_seconds=0, delete_grace_seconds=production_grace,
        delete_missing_scans=3, automatic_deletion_enabled=True, llm_enabled=False,
        allow_model_calls=False, reuse_robot1_artifacts=False, excluded_sources={})
    save(output / 'config.json', cfg)
    fixture = source / '自动删除验收.txt'
    fixture.write_text('机器人5自动删除隔离验收。唯一标记：' + output.name + '\n')
    worker = Sync(cfg)
    for _ in range(12):
        worker.cycle()
        rows = list(worker.state['records'].values())
        if rows and rows[0]['state'] == 'active':
            break
        time.sleep(2)
    else:
        raise RuntimeError('isolated_fixture_not_active')
    record = rows[0]
    remote_ids = list(record['remote_ids'])
    save(output / 'before-delete.json', worker.state)
    print(json.dumps({'stage': 'fixture_active', 'output': str(output)}, ensure_ascii=False), flush=True)
    fixture.unlink()
    worker.cycle()
    if worker.state['records'][next(iter(worker.state['records']))]['state'] != 'missing_hold':
        raise RuntimeError('first_missing_scan_not_held')
    save(output / 'first-missing-scan.json', worker.state)
    for _ in range(60):
        worker.cycle()
        if record['state'] == 'deleted' and not worker.state['retire']:
            break
        time.sleep(2)
    else:
        raise RuntimeError('isolated_fixture_deletion_pending')
    if (Path(cfg['state_dir']) / 'deletion-confirmations.json').exists():
        raise RuntimeError('unexpected_manual_confirmation')
    statuses = {identity: worker.api.request('/knowledge/' + identity)[0] for identity in remote_ids}
    if any(status != 404 for status in statuses.values()):
        raise RuntimeError('remote_deletion_not_verified')
    if Path(record['md_path']).exists():
        raise RuntimeError('markdown_not_removed')
    backups = {identity: (Path(cfg['state_dir']) / 'retired-backups' / identity / 'original.md').exists()
               for identity in remote_ids}
    if not all(backups.values()):
        raise RuntimeError('remote_backup_missing')
    code, response = worker.api.request('/knowledge-bases/' + cfg['knowledge_base_id'] + '/hybrid-search',
        json.dumps({'query_text': output.name, 'knowledge_ids': remote_ids, 'match_count': 5,
            'disable_vector_match': True, 'vector_threshold': 0, 'keyword_threshold': 0,
            'skip_context_enrichment': True}).encode(), {'Content-Type': 'application/json'})
    if code != 200 or response.get('data'):
        raise RuntimeError('removed_fixture_still_retrievable')
    proof = {'status': 'passed', 'manual_confirmation_used': False, 'isolated_grace_seconds': production_grace,
        'production_grace_seconds': production_grace, 'required_missing_scans': 3, 'remote_status': statuses,
        'markdown_removed': True, 'remote_backups': backups, 'retrieval_hits': 0}
    save(output / '验收结果.json', proof)
    print(json.dumps({'output': str(output), **proof}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
