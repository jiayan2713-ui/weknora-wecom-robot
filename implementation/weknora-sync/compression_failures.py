"""Persist preparation failures by path, original content and compressor version."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from compress_documents import algorithm_version
from reconcile import save

DETERMINISTIC = {
    'target_size_not_reached_without_resizing', 'encrypted_or_signed_pdf_not_supported',
    'signed_document_not_supported', 'office_archive_integrity_failed',
    'office_non_image_part_changed', 'missing_internal_relationship_target',
    'image_name_collision', 'image_dimensions_changed', 'pdf_text_or_geometry_changed',
    'pdf_render_dimensions_changed', 'pdf_render_validation_failed',
}


def failure_path(registry, relative, digest):
    key = '\0'.join([relative, digest, algorithm_version()])
    return Path(registry) / 'failures' / (hashlib.sha256(key.encode()).hexdigest() + '.json')


def retry_gate(cfg, registry, relative, digest, *, now=None):
    path = failure_path(registry, relative, digest)
    if not path.exists() or cfg.get('retry_failed'):
        return None
    row = json.loads(path.read_text())
    if row.get('resolved'):
        return None
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    blocked = row['deterministic'] or row['attempts'] >= cfg.get('compression_max_attempts', 3)
    if not blocked and now >= datetime.fromisoformat(row['next_retry_at']):
        return None
    return {'relative': relative, 'phase': 'compression_blocked' if blocked else 'retry_deferred',
            'error': row['error'], 'failure_record': str(path), 'attempts': row['attempts'],
            'original_bytes': row['original_bytes'], 'output_bytes': row.get('candidate_bytes'),
            'backup': row.get('backup'), 'next_retry_at': None if blocked else row['next_retry_at']}


def record_failure(cfg, registry, relative, digest, backup, candidate, error):
    path = failure_path(registry, relative, digest)
    previous = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    attempts = previous.get('attempts', 0) + 1
    deterministic = str(error) in DETERMINISTIC or type(error).__name__ in {
        'BadZipFile', 'XMLSyntaxError', 'UnidentifiedImageError', 'FileDataError'}
    attempt = {'at': now.isoformat(), 'error': str(error), 'error_type': type(error).__name__,
               'backup': str(backup), 'candidate': str(candidate)}
    row = {'relative': relative, 'original_sha256': digest, 'algorithm_version': algorithm_version(),
           'attempts': attempts, 'deterministic': deterministic, 'resolved': False,
           'original_bytes': backup.stat().st_size, 'candidate_bytes': candidate.stat().st_size if candidate.exists() else None,
           **attempt, 'history': [*previous.get('history', []), attempt],
           'next_retry_at': (now + timedelta(hours=cfg.get('compression_retry_hours', 24) * 2 ** min(attempts - 1, 5))).isoformat()}
    save(path, row)


def resolve_failure(registry, relative, digest):
    path = failure_path(registry, relative, digest)
    if path.exists():
        row = json.loads(path.read_text())
        row.update(resolved=True, resolved_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
        save(path, row)
