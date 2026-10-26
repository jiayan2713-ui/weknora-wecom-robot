"""Scoped backup-first WeDrive compression with resumable receipts.

Local client acknowledgements are evidence of sync, never cloud download proof.
"""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from zoneinfo import ZoneInfo

from compress_documents import compress, LIMIT, SUPPORTED, algorithm_version
from compression_failures import retry_gate, record_failure, resolve_failure
from compression_reporting import summarize, publish_daily
from compression_versions import receipt_path, validation_passed, confirm_download
from reconcile import hashes, save
from compression_client import ClientUnavailable, client_status

APPROVED_SOURCE_ROOT = Path('/Users/xinchengyuan/Library/Containers/com.tencent.WeWorkMac/Data/WeDrive/海那边/项目to咨询')
PENDING_PHASES = {'local_write_intent', 'local_replaced', 'uncertain', 'attention_required'}


def require_client(cfg):
    status = client_status(cfg)
    if not status['ready']:
        raise ClientUnavailable(status['reason'])
    return status


def require_worker_protocol(cfg, worker):
    if cfg.get('require_source_write_lock_protocol') is not True:
        return
    ready = Path(worker['state_dir']) / 'worker-ready.json'
    if not ready.exists() or json.loads(ready.read_text()).get('source_write_lock_protocol') != 1:
        raise ValueError('worker_source_write_protocol_not_ready')


def require_write_permission(cfg, metadata):
    policy = cfg.get('permission_cache_policy', 'strict')
    if policy not in {'strict', 'observe'}:
        raise ValueError('unknown_permission_cache_policy')
    raw = metadata.get('role_info_raw_json')
    role = json.loads(raw).get('role', {}) if raw else {}
    evidence = {'policy': policy, 'cached_auth': role.get('auth'),
                'cached_data_version': json.loads(raw).get('data_version') if raw else None,
                'live_permission_verified': False}
    # Client cache is not a live permission query. In observe mode it is retained
    # as evidence; successful write plus version/hash acknowledgement is still required.
    if policy == 'observe':
        return evidence
    if not raw:
        if cfg.get('require_client_write_permission') is True:
            raise ValueError('client_write_permission_unknown')
        return evidence
    # WeDrive's client enum: 1=download, 2=edit, 3=manage, 4=preview.
    # Upload-only/custom permissions are deliberately not assumed to allow updates.
    if role.get('auth') not in {2, 3}:
        raise ValueError('client_edit_permission_required')
    return evidence


def authorized_scope(relative):
    scope = Path(relative)
    if scope.is_absolute() or scope.parts not in {('测试',), ('售前文档',)}:
        raise ValueError('compression_scope_not_authorized')
    return scope


def receipt_source(root, row, scope_relative='测试'):
    scope = authorized_scope(scope_relative)
    relative = Path(row['relative'])
    if (row.get('source_root') != str(root) or relative.is_absolute() or '..' in relative.parts
            or not relative.is_relative_to(scope)):
        raise ValueError('receipt_source_path_escaped')
    source = root / relative
    if source.is_symlink() or not source.resolve().is_relative_to(root / scope):
        raise ValueError('receipt_source_path_escaped')
    return source


def scope_paths(cfg):
    root = Path(cfg['source_root']).resolve()
    if root != Path(cfg.get('authorized_source_root', APPROVED_SOURCE_ROOT)).resolve():
        raise ValueError('source_root_not_authorized')
    relative = authorized_scope(cfg['scope_relative'])
    scope = root / relative
    if scope.is_symlink() or not scope.resolve().is_relative_to(root):
        raise ValueError('scope_path_escaped')
    registry = Path(cfg['compression_registry']).resolve()
    if registry.is_relative_to(root):
        raise ValueError('backup_must_be_outside_sync_root')
    return root, scope, registry


def client_metadata(cfg, relative):
    database = Path(cfg['client_database'])
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT file_id,relativePath,size,md5,mtime,seq_id,history_cnt,transformState,space_id,role_info_raw_json '
            'FROM File WHERE roomName=? AND relativePath=?',
            (cfg['room_name'], relative)).fetchall()
    if len(rows) != 1:
        raise ValueError('client_file_identity_ambiguous')
    result = dict(rows[0])
    if cfg.get('client_space_id') and result['space_id'] != cfg['client_space_id']:
        raise ClientUnavailable('client_space_identity_mismatch')
    return result


def source_signature(source):
    stat = source.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def acquire_worker_lock(lock, timeout, *, clock=time.monotonic, pause=time.sleep, error_code='worker_busy_timeout'):
    deadline = clock() + timeout
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if clock() >= deadline:
                raise ValueError(error_code)
            pause(1)


def prepare(cfg, source, root, registry):
    scope = authorized_scope(cfg['scope_relative'])
    relative = str(source.relative_to(root))
    if cfg.get('require_client_write_permission') is True:
        require_write_permission(cfg, client_metadata(cfg, relative))
    digest = hashes(source)['sha256']
    # Reuse only the same original version; never repeat an uncertain write.
    for receipt in (registry / 'receipts').glob('*.json'):
        row = json.loads(receipt.read_text())
        if row.get('relative') == relative and row.get('original_sha256') == digest:
            if row.get('phase') in {'local_write_intent', 'local_replaced', 'uncertain'}:
                raise ValueError('previous_write_requires_reconciliation')
            if row.get('phase') == 'prepared':
                backup = registry / row['backup_relative']
                candidate = registry / row['candidate_relative']
                if hashes(backup)['sha256'] != digest or hashes(candidate)['sha256'] != row['output_sha256']:
                    raise ValueError('prepared_artifact_corrupted')
                return row
    before = source_signature(source)
    if source.is_symlink() or not source.resolve().is_relative_to(root / scope):
        raise ValueError('source_path_escaped')
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S_%f')
    job = registry / 'jobs' / stamp / source.relative_to(root).parent
    job.mkdir(parents=True, exist_ok=True)
    backup = job / (source.stem + '_' + stamp + source.suffix)
    shutil.copy2(source, backup)
    if hashes(backup)['sha256'] != digest or source_signature(source) != before:
        raise ValueError('source_changed_during_backup')
    candidate = job / source.name
    # Compression reads the snapshot, not a concurrently changing sync source.
    try:
        validation = compress(backup, candidate)
    except Exception as error:
        record_failure(cfg, registry, relative, digest, backup, candidate, error)
        raise
    if hashes(source)['sha256'] != digest or source_signature(source) != before:
        raise ValueError('source_changed_during_compression')
    row = {'schema_version': 1, 'phase': 'prepared', 'source_root': str(root),
           'algorithm_version': algorithm_version(),
           'relative': relative, 'original_sha256': digest,
           'output_sha256': hashes(candidate)['sha256'],
           'backup_relative': str(backup.relative_to(registry)),
           'candidate_relative': str(candidate.relative_to(registry)),
           'source_signature': before, 'created_at': stamp,
           'cloud_file_id': None, 'cloud_version': None,
           'validation': {**validation, 'status': 'passed',
                          'machine_validation_complete': not bool(validation.get('format_warnings'))}}
    try:
        row['client_before'] = client_metadata(cfg, relative)
        row['cloud_file_id'] = row['client_before']['file_id']
    except (KeyError, OSError, ValueError, sqlite3.Error) as error:
        row['client_metadata_error'] = str(error)
    receipt = receipt_path(registry, relative, row['output_sha256'])
    if receipt.exists():
        history = registry / 'receipt-history'
        history.mkdir(exist_ok=True)
        shutil.copy2(receipt, history / (receipt.stem + '_' + stamp + '.json'))
    save(receipt, row)
    resolve_failure(registry, relative, digest)
    return row


def write_local(cfg, row, root, registry):
    require_client(cfg)
    source = receipt_source(root, row, cfg['scope_relative'])
    path = receipt_path(registry, row['relative'], row['output_sha256'])
    if row['phase'] != 'prepared':
        raise ValueError('source_identity_or_phase_not_ready')
    # Until the configured sync acceptance, worker holds the new hash.
    worker = json.loads(Path(cfg['worker_config']).read_text())
    require_worker_protocol(cfg, worker)
    configured = [worker.get('compression_registry'), *worker.get('compression_additional_registries', [])]
    if str(registry) not in [str(Path(p).resolve()) for p in configured if p]:
        raise ValueError('worker_registry_not_configured')
    if cfg.get('acceptance_mode') != 'client_sync' and row['relative'] in worker.get('compression_local_test_sources', []):
        raise ValueError('local_adoption_before_cloud_readback_is_not_allowed')
    candidate = registry / row['candidate_relative']
    backup = registry / row['backup_relative']
    if not validation_passed(row['validation']) or row['validation'].get('format_warnings'):
        raise ValueError('candidate_validation_incomplete')
    if (hashes(backup)['sha256'] != row['original_sha256']
            or hashes(candidate)['sha256'] != row['output_sha256']
            or candidate.stat().st_size >= LIMIT):
        raise ValueError('write_artifact_integrity_failed')
    before = client_metadata(cfg, row['relative'])
    row['permission_cache_check'] = require_write_permission(cfg, before)
    local = hashes(source)
    if local['sha256'] != row['original_sha256']:
        raise ValueError('employee_update_detected')
    if ((row.get('cloud_file_id') and before['file_id'] != row['cloud_file_id'])
            or before['md5'] != local['md5'] or int(before['size']) != local['size']):
        raise ValueError('client_source_not_synced_or_changed')
    if source.is_symlink() or not source.resolve().is_relative_to(root / authorized_scope(cfg['scope_relative'])):
        raise ValueError('source_path_escaped')
    # Client startup may change timestamps/inodes without changing the original bytes.
    row['source_signature'] = source_signature(source)
    row['client_before'] = before
    row['cloud_file_id'] = before['file_id']
    with (Path(worker['state_dir']) / 'source-write.lock').open('a') as lock:
        acquire_worker_lock(lock, cfg.get('worker_lock_timeout_seconds', 180))
        require_client(cfg)
        row['permission_cache_check'] = require_write_permission(cfg, client_metadata(cfg, row['relative']))
        # Preserve inode, path and filename for the existing WeDrive client.
        with source.open('r+b') as target:
            fcntl.flock(target, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if source_signature(source) != row['source_signature'] or hashes(source)['sha256'] != row['original_sha256']:
                raise ValueError('employee_update_detected')
            row['phase'] = 'local_write_intent'
            row['local_write_started_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            save(path, row)
            try:
                with candidate.open('rb') as incoming:
                    shutil.copyfileobj(incoming, target)
                target.truncate()
                target.flush()
                os.fsync(target.fileno())
                if hashes(source)['sha256'] != row['output_sha256']:
                    raise ValueError('local_write_hash_mismatch')
            except Exception:
                row['phase'] = 'uncertain'
                save(path, row)
                raise
    row['phase'] = 'local_replaced'
    save(path, row)
    return row


def confirm_client_sync(cfg, row, root, registry, *, clock=time.monotonic, pause=time.sleep):
    require_client(cfg)
    source = receipt_source(root, row, cfg['scope_relative'])
    if row.get('phase') not in PENDING_PHASES | {'client_verified'}:
        raise ValueError('client_sync_write_not_started')
    deadline = clock() + cfg.get('client_sync_timeout_seconds', 180)
    local = hashes(source)
    if local['sha256'] != row['output_sha256']:
        raise ValueError('employee_update_detected_during_client_sync')
    signature = source_signature(source)
    while True:
        if source_signature(source) != signature:
            if hashes(source)['sha256'] != row['output_sha256']:
                raise ValueError('employee_update_detected_during_client_sync')
            signature = source_signature(source)
        meta = client_metadata(cfg, row['relative'])
        if meta['file_id'] != row['cloud_file_id']:
            raise ValueError('client_file_identity_changed')
        before = row.get('client_before', {})
        advanced = (meta.get('history_cnt', 0) > before.get('history_cnt', 0) or
                    (bool(meta.get('seq_id')) and meta.get('seq_id') != before.get('seq_id')))
        matches = (meta['file_id'] == row['cloud_file_id'] and meta['md5'] == local['md5'] and
                   int(meta['size']) == local['size'])
        if matches and advanced:
            if hashes(source)['sha256'] != row['output_sha256']:
                raise ValueError('employee_update_detected_during_client_sync')
            row.update(phase='client_verified', acceptance='client_sync',
                       client_sync_evidence={'file_id': meta['file_id'], 'local_sha256': local['sha256'],
                         'md5': local['md5'], 'bytes': local['size'],
                         'metadata_matches_output': True, 'version_record_advanced': True,
                         'client_seq_id': meta.get('seq_id'), 'client_history_count': meta.get('history_cnt'),
                         'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                         'independent_cloud_download': False})
            save(receipt_path(registry, row['relative'], row['output_sha256']), row)
            return row
        if clock() >= deadline:
            raise ValueError('client_sync_acknowledgement_timeout')
        require_client(cfg)
        pause(3)


def recover_receipt(cfg, row, root, registry):
    """Check first; at most once signal a known unchanged compressed version."""
    require_client(cfg)
    source = receipt_source(root, row, cfg['scope_relative'])
    local = hashes(source)
    path = receipt_path(registry, row['relative'], row['output_sha256'])
    if row['phase'] == 'prepared':
        write_local(cfg, row, root, registry)
        return confirm_client_sync(cfg, row, root, registry)
    if (row['phase'] == 'local_write_intent' and local['sha256'] == row['original_sha256']
            and source_signature(source) == row['source_signature']):
        row.update(phase='prepared', recovery_action='write_not_observed')
        save(path, row)
        write_local(cfg, row, root, registry)
        return confirm_client_sync(cfg, row, root, registry)
    if local['sha256'] != row['output_sha256']:
        raise ValueError('employee_update_detected_during_client_sync')
    meta = client_metadata(cfg, row['relative'])
    before = row.get('client_before', {})
    if meta['file_id'] != row.get('cloud_file_id'):
        raise ValueError('client_file_identity_changed')
    matches = meta['md5'] == local['md5'] and int(meta['size']) == local['size']
    if matches:
        # No event is emitted for an already acknowledged version.
        return confirm_client_sync(cfg, row, root, registry)
    if meta['md5'] != before.get('md5') or int(meta['size']) != int(before.get('size', -1)):
        raise ValueError('client_version_conflict')
    if row.get('recovery_attempts', 0) >= cfg.get('recovery_max_attempts', 3):
        row.update(phase='attention_required', last_error='recovery_attempt_limit_reached')
        save(path, row)
        return row
    row['recovery_attempts'] = row.get('recovery_attempts', 0) + 1
    save(path, row)
    if row.get('retrigger_count', 0) < cfg.get('recovery_max_retriggers', 1):
        backup = (registry / row['backup_relative']).resolve()
        if not backup.is_relative_to(registry) or hashes(backup)['sha256'] != row['original_sha256']:
            raise ValueError('recovery_backup_integrity_failed')
        if not validation_passed(row.get('validation', {})):
            raise ValueError('candidate_validation_incomplete')
        worker = json.loads(Path(cfg['worker_config']).read_text())
        require_worker_protocol(cfg, worker)
        require_write_permission(cfg, meta)
        with (Path(worker['state_dir']) / 'source-write.lock').open('a') as lock:
            acquire_worker_lock(lock, cfg.get('worker_lock_timeout_seconds', 180))
            require_client(cfg)
            with source.open('rb') as file:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if hashes(source)['sha256'] != row['output_sha256']:
                    raise ValueError('employee_update_detected_during_client_sync')
                row.update(retrigger_count=row.get('retrigger_count', 0) + 1,
                           retriggered_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                           retrigger_action='mtime_only_intent')
                save(path, row)
                os.utime(file.fileno(), None)
                if hashes(source)['sha256'] != row['output_sha256']:
                    raise ValueError('employee_update_detected_during_client_sync')
                row['retrigger_action'] = 'mtime_only_completed'
                save(path, row)
    return confirm_client_sync(cfg, row, root, registry)


def process_receipt(cfg, row, root, registry, *, recovery):
    item = {'relative': row['relative'], 'receipt': str(receipt_path(registry, row['relative'], row['output_sha256']))}
    if row.get('backup_relative'):
        backup = registry / row['backup_relative']
        item['backup'] = str(backup)
        if backup.is_file():
            item['original_bytes'] = backup.stat().st_size
    try:
        if recovery:
            recover_receipt(cfg, row, root, registry)
        else:
            write_local(cfg, row, root, registry)
            confirm_client_sync(cfg, row, root, registry)
        item.update(phase=row['phase'], output_bytes=row['validation']['output_bytes'])
        if row['phase'] == 'attention_required':
            item['error'] = row.get('last_error')
        else:
            row.pop('last_error', None)
    except ClientUnavailable as error:
        item.update(phase='waiting_client', error=str(error))
        row['last_error'] = str(error)
    except (OSError, ValueError, sqlite3.Error) as error:
        message = str(error)
        if message in {'client_sync_acknowledgement_timeout', 'client_file_identity_ambiguous',
                       'client_source_not_synced_or_changed', 'worker_busy_timeout'} or isinstance(error, sqlite3.Error):
            item.update(phase='waiting_sync', error=message)
        elif (message.startswith('employee_update_') or message in {'client_file_identity_changed',
              'client_version_conflict', 'receipt_source_path_escaped'} or isinstance(error, FileNotFoundError)):
            row['phase'] = 'conflict'
            item.update(phase='conflict', error=message)
        else:
            item.update(phase='failed', error=message, error_type=type(error).__name__)
        row['last_error'] = message
    row['last_checked_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    save(Path(item['receipt']), row)
    return item


def run(cfg, apply=False, *, resume_only=False, retry_failed=False):
    if apply and cfg.get('apply_enabled', True) is not True:
        raise ValueError('compression_apply_disabled')
    if retry_failed:
        cfg = {**cfg, 'retry_failed': True}
    if resume_only:
        cfg = {**cfg, 'client_sync_timeout_seconds': cfg.get('recovery_sync_timeout_seconds', 60)}
    root, scope, registry = scope_paths(cfg)
    registry.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo('Asia/Shanghai'))
    report = {'schema_version': 2, 'run_id': stamp.strftime('%Y%m%d_%H%M%S_%f') + '_' + str(os.getpid()),
              'started_at': stamp.isoformat(), 'scope': str(scope), 'apply': apply,
              'mode': 'recovery' if resume_only else 'daily', 'status': 'running', 'files': [],
              'scanned_files': 0, 'eligible_files': 0, 'counts': {}, 'inventory': [],
              'algorithm_version': algorithm_version(), 'cloud_verified': False,
              'acceptance_mode': cfg.get('acceptance_mode', 'independent_cloud_readback')}
    lock = (registry / 'batch.lock').open('a')
    if apply and not resume_only:
        save(registry / 'active-daily.json', {**report, 'pid': os.getpid()})
    try:
        try:
            if resume_only:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                acquire_worker_lock(lock, cfg.get('batch_lock_timeout_seconds', 300), error_code='batch_busy_timeout')
        except BlockingIOError:
            report['status'] = 'busy'
            return report
        if not scope.is_dir():
            raise ValueError('scope_missing')
        client_mode = cfg.get('acceptance_mode') == 'client_sync'
        if apply and not client_mode and not cfg.get('cloud_round_trip_accepted', False):
            raise ValueError('batch_activation_requires_successful_cloud_round_trip')
        rows = []
        for path in sorted((registry / 'receipts').glob('*.json')):
            try:
                rows.append(json.loads(path.read_text()))
            except (OSError, ValueError) as error:
                report['files'].append({'receipt': str(path), 'phase': 'failed', 'error': type(error).__name__})
        if apply and not client_mode and not any(r.get('phase') == 'cloud_verified' and r.get('readback_evidence') for r in rows):
            raise ValueError('cloud_round_trip_evidence_missing')
        handled = set()
        if apply and client_mode:
            report['client'] = client_status(cfg)
            checked = 0
            for row in rows:
                if row.get('phase') == 'conflict':
                    handled.add(row['relative'])
                    report['files'].append({'relative': row['relative'], 'phase': 'conflict', 'error': row.get('last_error')})
                elif row.get('phase') in PENDING_PHASES | {'prepared'}:
                    if resume_only and checked >= cfg.get('recovery_max_files_per_run', 3):
                        report['remaining_receipts'] = report.get('remaining_receipts', 0) + 1
                        continue
                    checked += 1
                    handled.add(row['relative'])
                    report['files'].append(process_receipt(cfg, row, root, registry, recovery=True))
        if not resume_only:
            for source in sorted(scope.rglob('*')):
                if source.is_symlink():
                    report['inventory'].append({'relative': str(source.relative_to(root)), 'phase': 'symlink'})
                    continue
                if not source.is_file():
                    continue
                report['scanned_files'] += 1
                relative = str(source.relative_to(root))
                size = source.stat().st_size
                if relative in handled:
                    continue
                reason = ('metadata' if source.name in {'.DS_Store', '.WeDrive'} or source.name.startswith('~$') else
                          'excluded' if relative in cfg.get('excluded_paths', []) else
                          'unsupported' if source.suffix.lower() not in SUPPORTED else
                          'not_needed' if size <= LIMIT else None)
                if reason:
                    report['inventory'].append({'relative': relative, 'phase': reason,
                                                'original_bytes': size})
                    continue
                report['eligible_files'] += 1
                try:
                    held = retry_gate(cfg, registry, relative, hashes(source)['sha256'])
                    if held:
                        report['files'].append(held)
                        continue
                    row = prepare(cfg, source, root, registry)
                    item = process_receipt(cfg, row, root, registry, recovery=False) if apply else {
                        'relative': relative, 'phase': 'prepared', 'output_bytes': row['validation']['output_bytes']}
                    item.update(original_bytes=size, backup=str(registry / row['backup_relative']))
                except Exception as error:
                    item = {'relative': relative, 'phase': 'failed', 'original_bytes': size,
                            'error': str(error), 'error_type': type(error).__name__}
                    try:
                        held = retry_gate({**cfg, 'retry_failed': False}, registry, relative, hashes(source)['sha256'])
                        if held:
                            item.update({k: v for k, v in held.items() if k != 'phase'})
                    except (OSError, ValueError) as detail_error:
                        item['failure_detail_error'] = str(detail_error)
                report['files'].append(item)
        phases = [item['phase'] for item in report['files']]
        report['counts'] = {phase: phases.count(phase) for phase in sorted(set(phases))}
        report['status'] = ('needs_attention' if any(p in phases for p in ['failed', 'conflict', 'attention_required', 'compression_blocked'])
                            else 'waiting' if report.get('remaining_receipts') or any(p in phases for p in ['waiting_client', 'waiting_sync', 'retry_deferred', 'prepared']) else 'success')
    except Exception as error:
        report.update(status='failed', error=str(error), error_type=type(error).__name__)
    finally:
        try:
            report['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            report['duration_seconds'] = round((datetime.fromisoformat(report['finished_at']) - stamp).total_seconds(), 3)
            report['summary'] = summarize(report)
            if apply and not resume_only:
                active = registry / 'active-daily.json'
                if active.exists() and json.loads(active.read_text()).get('run_id') == report['run_id']:
                    save(active, {**report, 'pid': os.getpid()})
            save(registry / 'runs' / (report['run_id'] + '.json'), report)
            if report['status'] != 'busy':
                save(registry / 'last-run.json', report)
                save(registry / ('last-' + report['mode'] + '.json'), report)
                publish_daily(registry, report)
        finally:
            lock.close()
    return report


def accept_readback(cfg, receipt, downloaded, file_id, version):
    root, _, registry = scope_paths(cfg)
    downloaded = Path(downloaded).resolve()
    receipt = Path(receipt)
    if receipt.is_symlink():
        raise ValueError('receipt_path_escaped')
    receipt = receipt.resolve()
    if not receipt.is_relative_to(registry / 'receipts'):
        raise ValueError('receipt_path_escaped')
    if downloaded.is_relative_to(root) or downloaded.is_relative_to(registry):
        raise ValueError('readback_must_not_be_local_source_or_candidate')
    row = json.loads(receipt.read_text())
    if not validation_passed(row.get('validation', {})) or row['validation'].get('format_warnings'):
        raise ValueError('readback_candidate_validation_incomplete')
    source = root / row['relative']
    if hashes(source)['sha256'] != row['output_sha256']:
        raise ValueError('employee_update_detected_during_readback')
    result = confirm_download(registry, row['relative'], row['output_sha256'], downloaded,
                              file_id=file_id, version=version)
    result['readback_evidence'] = {'downloaded_path': str(downloaded),
                                 'downloaded_sha256': hashes(downloaded)['sha256'],
                                 'cloud_version_kind': 'upstream_supplied'}
    save(receipt, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume-only', action='store_true', help='Only inspect existing staged/pending receipts')
    parser.add_argument('--retry-failed', action='store_true', help='Explicitly retry held preparation failures once in this run')
    parser.add_argument('--confirm-receipt')
    parser.add_argument('--downloaded-file')
    parser.add_argument('--cloud-file-id')
    parser.add_argument('--cloud-version')
    args = parser.parse_args()
    started = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    try:
        cfg = json.loads(Path(args.config).read_text())
        if args.confirm_receipt:
            if not all((args.downloaded_file, args.cloud_file_id, args.cloud_version)):
                parser.error('confirm requires independently downloaded file, cloud ID and upstream version')
            accept_readback(cfg, args.confirm_receipt, args.downloaded_file, args.cloud_file_id, args.cloud_version)
            return
        result = run(cfg, args.apply, resume_only=args.resume_only, retry_failed=args.retry_failed)
    except Exception as error:
        result = {'status': 'failed', 'started_at': started,
                  'finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                  'error': str(error), 'error_type': type(error).__name__, 'config': args.config}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result['status'] in {'failed', 'needs_attention'}:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
