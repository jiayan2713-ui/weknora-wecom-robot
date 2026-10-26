"""Opt-in compression provenance; cloud receipts never follow from local writes."""
import hashlib
import json
from pathlib import Path
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

from reconcile import hashes, save


def receipt_path(directory, relative, digest):
    key = hashlib.sha256((relative + '\0' + digest).encode()).hexdigest()
    return Path(directory) / 'receipts' / (key + '.json')


def validation_passed(validation):
    machine = validation.get('machine_validation_complete') and validation.get('validation') in {
        'text_tables_layout_parts_byte_identical', 'page_text_and_geometry_equal'}
    return validation.get('status') == 'passed' and (
        validation.get('visual_review_complete') or machine)


def stage(cfg, relative, compressed, validation):
    """Backup and stage an inspected candidate; never modify the source or cloud."""
    root = Path(cfg['source_root']).resolve()
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('compression_source_path_escaped')
    source = root / relative
    if source.is_symlink() or not source.resolve().is_relative_to(root):
        raise ValueError('compression_source_path_escaped')
    original_hash = hashes(source)['sha256']
    if original_hash != validation.get('original_sha256_before') or not validation.get('original_unchanged'):
        raise ValueError('compression_validation_source_mismatch')
    compressed = Path(compressed)
    output_hash = hashes(compressed)['sha256']
    if compressed.stat().st_size >= 30_000_000 or output_hash != validation.get('output_sha256'):
        raise ValueError('compression_candidate_validation_mismatch')
    if validation.get('status') != 'passed' or not validation.get('visual_review_complete'):
        raise ValueError('compression_candidate_not_reviewed')
    directory = Path(cfg['compression_registry']).resolve()
    if directory.is_relative_to(root):
        raise ValueError('compression_backup_must_be_outside_source')
    receipt = receipt_path(directory, relative, output_hash)
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous.get('original_sha256') != original_hash or previous.get('source_root') != str(root):
            raise ValueError('compression_receipt_collision')
        for field, expected in [('backup_relative', original_hash), ('candidate_relative', output_hash)]:
            existing = (directory / previous[field]).resolve()
            if not existing.is_relative_to(directory) or hashes(existing)['sha256'] != expected:
                raise ValueError('compression_staging_integrity_failed')
        return previous
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S_%f')
    job = directory / 'jobs' / (hashlib.sha256(relative.encode()).hexdigest()[:24] + '_' + stamp)
    job.mkdir(parents=True, mode=0o700)
    backup = job / (source.stem + '_' + stamp + source.suffix)
    candidate = job / source.name
    shutil.copy2(source, backup)
    shutil.copy2(compressed, candidate)
    if hashes(backup)['sha256'] != original_hash or hashes(source)['sha256'] != original_hash or \
            hashes(candidate)['sha256'] != output_hash:
        raise ValueError('compression_staging_integrity_failed')
    row = {'schema_version': 1, 'phase': 'prepared', 'source_root': str(root),
           'relative': relative, 'original_sha256': original_hash, 'output_sha256': output_hash,
           'backup_relative': str(backup.relative_to(directory)),
           'candidate_relative': str(candidate.relative_to(directory)),
           'cloud_file_id': None, 'cloud_version': None, 'created_at': stamp,
           'validation': {'status': 'passed', 'visual_review_complete': True,
                          'report_path': validation.get('report_path')}}
    save(receipt, row)
    return row


def publish(cfg, relative, digest, transport):
    """Transport must enforce atomic expected-version replacement and real readback."""
    directory = Path(cfg['compression_registry']).resolve()
    path = receipt_path(directory, relative, digest)
    row = json.loads(path.read_text())
    if row['phase'] != 'prepared' or not row.get('cloud_file_id') or not row.get('expected_cloud_version'):
        raise ValueError('compression_target_not_bound_or_upload_uncertain')
    current = transport.inspect(row['cloud_file_id'])
    if current['version'] != row['expected_cloud_version'] or current['sha256'] != row['original_sha256']:
        raise ValueError('compression_cloud_source_changed')
    source = Path(cfg['source_root']) / relative
    if hashes(source)['sha256'] != row['original_sha256']:
        raise ValueError('compression_local_source_changed')
    candidate = (directory / row['candidate_relative']).resolve()
    backup = (directory / row['backup_relative']).resolve()
    if not candidate.is_relative_to(directory) or not backup.is_relative_to(directory) or \
            hashes(candidate)['sha256'] != digest or hashes(backup)['sha256'] != row['original_sha256']:
        raise ValueError('compression_staging_integrity_failed')
    row['phase'] = 'upload_intent'
    save(path, row)
    try:
        transport.replace(row['cloud_file_id'], candidate, expected_version=row['expected_cloud_version'])
        downloaded, version = transport.download(row['cloud_file_id'])
        return confirm_download(directory, relative, digest, downloaded,
                                file_id=row['cloud_file_id'], version=version)
    except Exception:
        row['phase'] = 'uncertain'
        save(path, row)
        raise


def verified_receipt(cfg, relative, digest, *, allow_local=False):
    directory = cfg.get('compression_registry')
    if not directory:
        return None
    matches = [(Path(d).resolve(), receipt_path(d, relative, digest))
               for d in [directory, *cfg.get('compression_additional_registries', [])]
               if receipt_path(d, relative, digest).exists()]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('compression_receipt_identity_ambiguous')
    base, path = matches[0]
    row = json.loads(path.read_text())
    local_test = allow_local and relative in cfg.get('compression_local_test_sources', []) and \
        row.get('phase') in ('local_write_intent', 'local_replaced')
    client_scope = any(Path(relative).is_relative_to(Path(scope)) and
                       not Path(scope).is_absolute() and '..' not in Path(scope).parts
                       for scope in cfg.get('compression_client_sync_roots', []))
    client_sync = row.get('phase') == 'client_verified' and client_scope
    if client_sync:
        evidence = row.get('client_sync_evidence', {})
        if (evidence.get('local_sha256') != digest or
                evidence.get('file_id') != row.get('cloud_file_id') or
                not evidence.get('metadata_matches_output') or
                not evidence.get('version_record_advanced')):
            raise ValueError('compression_client_sync_evidence_missing')
    if row.get('phase') != 'cloud_verified' and not local_test and not client_sync:
        raise ValueError('compression_cloud_verification_pending')
    if row.get('source_root') != str(Path(cfg['source_root']).resolve()) or \
            row.get('relative') != relative or row.get('output_sha256') != digest:
        raise ValueError('compression_receipt_identity_mismatch')
    if not local_test and (not row.get('cloud_file_id') or
                          (not client_sync and not row.get('cloud_version'))):
        raise ValueError('compression_cloud_identity_missing')
    backup = (base / row['backup_relative']).resolve()
    if not backup.is_relative_to(base) or backup.is_symlink() or \
            hashes(backup)['sha256'] != row['original_sha256']:
        raise ValueError('compression_backup_integrity_failed')
    if not validation_passed(row.get('validation', {})):
        raise ValueError('compression_validation_missing')
    return {**row, 'backup_path': str(backup), 'receipt_path': str(path)}


def confirm_download(directory, relative, digest, downloaded, *, file_id, version):
    """Record a real cloud readback supplied by an upstream transport adapter."""
    path = receipt_path(directory, relative, digest)
    row = json.loads(path.read_text())
    if not file_id or not version or file_id != row.get('cloud_file_id'):
        raise ValueError('compression_cloud_identity_mismatch')
    if row.get('phase') not in ('upload_intent', 'local_replaced', 'uncertain', 'cloud_verified'):
        raise ValueError('compression_upload_not_started')
    if hashes(Path(downloaded))['sha256'] != digest:
        raise ValueError('compression_cloud_readback_mismatch')
    row.update(phase='cloud_verified', cloud_version=version)
    save(path, row)
    return row


def adopt_active(cfg, record, relative, digest, docs):
    if not cfg.get('compression_registry') or record.get('source_hash') == digest:
        return None
    receipt = verified_receipt(cfg, relative, digest, allow_local=True)
    if not receipt or record.get('pending') or record.get('parse_retry_requested') or \
            record.get('source_hash') != receipt['original_sha256'] or record['path'] != relative:
        return None
    markdown = Path(record['md_path']) if record.get('md_path') else None
    if not markdown or not markdown.exists() or hashes(markdown)['sha256'] != record.get('md_sha'):
        return None
    remote = {d['id']: d for d in docs}
    ids = record.get('remote_ids', [])
    if not ids or any(i not in remote or remote[i].get('parse_status') != 'completed' or
                      remote[i].get('enable_status') != 'enabled' or
                      remote[i].get('pending_subtasks_count') for i in ids):
        return None
    record.update(source_hash=digest, compression=receipt, state='active')
    if record.get('error') in ('compression_cloud_verification_pending',
                              'ValueError:compression_cloud_verification_pending'):
        record.pop('error', None)
        record.pop('next_attempt', None)
    return receipt


def parsing_source(cfg, source, relative, digest):
    receipt = verified_receipt(cfg, relative, digest)
    if not receipt:
        return source, digest, None
    return Path(receipt['backup_path']), receipt['original_sha256'], receipt
