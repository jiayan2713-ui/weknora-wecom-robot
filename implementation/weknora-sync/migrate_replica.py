"""Offline, checked state migration preserving source and knowledge identities."""
import argparse
import copy
import hashlib
from pathlib import Path
import time

from replica_protocol import (ProtocolError, copy_verified, digest, file_hash, identity,
                              inside, locked, read, replica_state, sync_directory, write)


def md5(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'md5').hexdigest()


def migrate(source_cfg, target_cfg, inventory, *, source_stopped=False,
            state_snapshot=None, markdown_snapshot=None):
    if not source_stopped:
        raise ProtocolError('stop_source_worker_before_migration')
    if source_cfg['knowledge_base_id'] != target_cfg['knowledge_base_id'] or \
            source_cfg['folder_root'] != target_cfg['folder_root']:
        raise ProtocolError('migration_must_preserve_knowledge_base_and_scope')
    target_cfg = copy.deepcopy(target_cfg)
    exclusions = source_cfg.get('excluded_sources', {})
    if target_cfg.get('excluded_sources', exclusions) != exclusions:
        raise ProtocolError('migration_exclusion_policy_changed')
    target_cfg['excluded_sources'] = copy.deepcopy(exclusions)
    source_parser = source_cfg.get('parser_config', {})
    target_parser = target_cfg.get('parser_config', {})
    if any(k in source_parser and k in target_parser and source_parser[k] != target_parser[k]
           for k in ('model', 'endpoint', 'model_review')):
        raise ProtocolError('migration_parser_policy_changed')
    target_cfg['parser_config'] = {**source_parser, **target_parser}
    for key in ('required_capabilities', 'model_authorization', 'reuse_robot1_artifacts'):
        if key not in target_cfg and key in source_cfg:
            target_cfg[key] = copy.deepcopy(source_cfg[key])
    old_home = Path(source_cfg['state_dir']).resolve()
    old_md = Path(source_cfg['markdown_root']).resolve()
    snapshot_home = Path(state_snapshot).resolve() if state_snapshot else old_home
    snapshot_md = Path(markdown_snapshot).resolve() if markdown_snapshot else old_md
    new_home = Path(target_cfg['state_dir']).resolve()
    new_md = Path(target_cfg['markdown_root']).resolve()
    new_root = Path(target_cfg['source_root']).resolve()
    if new_home == old_home or new_home.is_relative_to(snapshot_home) or snapshot_home.is_relative_to(new_home):
        raise ProtocolError('migration_state_directories_overlap')
    if new_md == old_md or any(a.is_relative_to(b) or b.is_relative_to(a) for a, b in
                              [(new_md, new_root), (new_home, new_root), (new_md, new_home)]):
        raise ProtocolError('migration_target_directories_overlap')
    with locked(snapshot_home / 'worker.lock'), locked(new_home / 'worker.lock'):
        marker = new_home / 'migration.incomplete.json'
        if (new_home / 'state.json').exists():
            if marker.exists():
                commit = read(marker)
                current = read(new_home / 'state.json')
                report = read(new_home / 'migration-report.json')
                if commit.get('state_sha256') == digest(current) and commit.get('report_sha256') == digest(report) and \
                        current['identity'] == [str(Path(target_cfg['source_root']).resolve()),
                        target_cfg['knowledge_base_id'], target_cfg['weknora_base_url']]:
                    marker.unlink()
                    sync_directory(new_home)
                    return report
            raise ProtocolError('target_worker_state_already_exists')
        state = read(snapshot_home / 'state.json')
        if state['identity'] != [str(Path(source_cfg['source_root']).resolve()),
                                  source_cfg['knowledge_base_id'], source_cfg['weknora_base_url']]:
            raise ProtocolError('source_state_binding_mismatch')
        if any(r.get('pending') for r in state['records'].values()):
            raise ProtocolError('resolve_pending_uploads_before_migration')
        if target_cfg.get('source_mode') != 'replica':
            raise ProtocolError('target_must_use_replica_mode')
        replica = replica_state(target_cfg)
        documents = {doc['id']: doc for doc in inventory}
        transfers = []
        for record in state['records'].values():
            if record['state'] != 'deleted':
                name = record.get('desired_path') or record['path']
                entry = replica['files'].get(name)
                source = inside(target_cfg['source_root'], name)
                expected = record.get('latest_observed_hash') or record.get('blocked_source_hash') or record.get('source_hash')
                if not entry or not source.is_file() or file_hash(source) != entry['sha256'] or \
                        expected and entry['sha256'] != expected:
                    raise ProtocolError('source_replica_does_not_match_mapping')
            if not record.get('md_path'):
                continue
            path = Path(record['md_path'])
            if not path.is_relative_to(old_md):
                raise ProtocolError('mapped_markdown_outside_original_root')
            name = str(path.relative_to(old_md))
            source = inside(snapshot_md, name)
            # Deleted records retain historical paths but the current file is absent.
            if record['state'] == 'deleted':
                continue
            if not source.is_file() or file_hash(source) != record['md_sha']:
                raise ProtocolError('mapped_markdown_hash_mismatch')
            for identifier in record.get('remote_ids', []):
                doc = documents.get(identifier)
                folder = (doc or {}).get('folder_path', '').strip('/')
                scope = target_cfg['folder_root'].strip('/')
                if not doc or doc.get('knowledge_base_id') != target_cfg['knowledge_base_id'] or \
                        folder != scope and not folder.startswith(scope + '/') or \
                        doc.get('file_hash') != md5(source) or doc.get('file_size') != source.stat().st_size:
                    raise ProtocolError('remote_knowledge_identity_or_content_mismatch')
                if doc.get('parse_status') != 'completed' or doc.get('enable_status') != 'enabled' or doc.get('pending_subtasks_count'):
                    raise ProtocolError('remote_knowledge_not_ready')
            transfers.append((source, inside(new_md, name), record['md_sha'], source.stat().st_size))
        for folder in ('artifacts', 'history', 'retired-backups', 'jobs'):
            for source in (snapshot_home / folder).rglob('*'):
                if source.is_symlink():
                    raise ProtocolError('symlink_in_migration_assets')
                if source.is_file():
                    target = inside(new_home, str(source.relative_to(snapshot_home)))
                    transfers.append((source, target, file_hash(source), source.stat().st_size))
        plan = {'source_identity': state['identity'], 'target_identity': [str(Path(target_cfg['source_root']).resolve()),
                target_cfg['knowledge_base_id'], target_cfg['weknora_base_url']],
                'assets': [(str(target), checksum) for _, target, checksum, _ in transfers]}
        if marker.exists() and read(marker)['plan_sha256'] != digest(plan):
            raise ProtocolError('incomplete_migration_plan_changed')
        write(marker, {'plan_sha256': digest(plan)})
        for source, target, checksum, size in transfers:
            if target.exists():
                if file_hash(target) != checksum:
                    raise ProtocolError('migration_target_asset_conflict')
            else:
                copy_verified(source, target, checksum, size)
        migrated = copy.deepcopy(state)
        migrated.update(identity=plan['target_identity'], root_identity=identity(target_cfg['source_root']),
                        cache={}, seeded=True)
        def rebind(value, old, new):
            path = Path(value)
            return str(new / path.relative_to(old)) if path.is_relative_to(old) else value
        for record in migrated['records'].values():
            for key in ('md_path',):
                if record.get(key):
                    record[key] = rebind(record[key], old_md, new_md)
            for key in ('cached_md', 'latest_parse_report_path'):
                if record.get(key):
                    record[key] = rebind(record[key], old_home, new_home)
            provenance = record.get('provenance')
            if isinstance(provenance, dict):
                for key in ('parse_report_path', 'original_snapshot'):
                    if provenance.get(key):
                        provenance[key] = rebind(provenance[key], old_home, new_home)
            record.pop('missing_since', None)
            record['missing_scans'] = 0
        migrated['events'] = (migrated.get('events', []) + [{'time': time.time(), 'kind': 'replica_state_migrated'}])[-1000:]
        report = {'status': 'verified', 'records_preserved': len(state['records']),
                  'source_ids_preserved': list(state['records']) == list(migrated['records']),
                  'knowledge_ids_preserved': all(r.get('remote_ids') == migrated['records'][key].get('remote_ids')
                                                 for key, r in state['records'].items()),
                  'assets_verified': len(transfers), 'source_snapshot_seq': replica['last_seq'],
                  'exclusion_policy_preserved': True,
                  'effective_target_config': str(new_home / 'effective-target-config.json'),
                  'historical_jobs': 'retained_as_evidence_not_forced_cross_platform_resume'}
        write(new_home / 'effective-target-config.json', target_cfg)
        write(new_home / 'migration-report.json', report)
        write(marker, {'plan_sha256': digest(plan), 'state_sha256': digest(migrated), 'report_sha256': digest(report)})
        write(new_home / 'state.json', migrated)
        marker.unlink()
        sync_directory(new_home)
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-config', type=Path, required=True)
    parser.add_argument('--target-config', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--source-stopped', action='store_true')
    parser.add_argument('--state-snapshot', type=Path)
    parser.add_argument('--markdown-snapshot', type=Path)
    args = parser.parse_args()
    report = migrate(read(args.source_config), read(args.target_config), read(args.inventory),
                     source_stopped=args.source_stopped, state_snapshot=args.state_snapshot,
                     markdown_snapshot=args.markdown_snapshot)
    print('迁移核验通过；来源和知识 ID 已保留：', report['records_preserved'])


if __name__ == '__main__':
    main()
