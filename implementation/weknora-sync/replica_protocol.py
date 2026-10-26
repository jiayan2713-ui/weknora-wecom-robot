"""Small, durable file-batch protocol shared by collector, receiver and worker."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata

VERSION = 1


class ProtocolError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read(path):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ProtocolError('protocol_json_capacity_exceeded')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError('duplicate_json_key')
            result[key] = value
        return result
    def invalid(_):
        raise ProtocolError('nonfinite_json')
    def number(value):
        value = float(value)
        if not math.isfinite(value):
            raise ProtocolError('nonfinite_json')
        return value
    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ProtocolError('invalid_protocol_json') from error


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ProtocolError('invalid_scope_or_generation')
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ProtocolError('invalid_sha256')
    return value


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ProtocolError('unsafe_relative_path')
    result = unicodedata.normalize('NFC', value)
    parts = result.split('/')
    if PurePosixPath(result).is_absolute() or any(p in ('', '.', '..') or p.startswith('.') for p in parts):
        raise ProtocolError('unsafe_relative_path')
    return result


def inside(root, name):
    root = Path(root).resolve()
    path = root / relative(name)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ProtocolError('symlink_in_replica_path')
    if not path.resolve().is_relative_to(root):
        raise ProtocolError('replica_path_escape')
    return path


def identity(root):
    stat = Path(root).stat()
    return [stat.st_dev, stat.st_ino]


def signature(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def file_hash(path):
    before = signature(path)
    with Path(path).open('rb') as stream:
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
    if before != signature(path):
        raise ProtocolError('source_changed_during_read')
    return result


def copy_verified(source, destination, expected, size):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.part-', dir=destination.parent)
    try:
        checksum = hashlib.sha256()
        actual_size = 0
        with Path(source).open('rb') as incoming, os.fdopen(fd, 'wb') as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                checksum.update(block)
                actual_size += len(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if actual_size != size or checksum.hexdigest() != expected:
            raise ProtocolError('transferred_object_hash_or_size_mismatch')
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ProtocolError('unsupported_batch_version')
    token(value.get('scope_id')); token(value.get('generation'))
    if type(value.get('seq')) is not int or value['seq'] < 1 or value.get('kind') not in ('baseline', 'delta'):
        raise ProtocolError('invalid_batch_sequence')
    names = set()
    for field in ('upserts', 'deletes'):
        if not isinstance(value.get(field), list):
            raise ProtocolError('invalid_batch_operations')
        for operation in value[field]:
            if not isinstance(operation, dict):
                raise ProtocolError('invalid_batch_operation')
            name = relative(operation.get('path'))
            if name != operation['path'] or name in names:
                raise ProtocolError('duplicate_or_noncanonical_batch_path')
            names.add(name)
            sha(operation.get('sha256') if field == 'upserts' else operation.get('expected_hash'))
            if field == 'upserts' and (type(operation.get('size')) is not int or operation['size'] < 0):
                raise ProtocolError('invalid_object_size')
            if field == 'deletes' and (type(operation.get('missing_scans')) is not int or
                    operation['missing_scans'] < 3 or type(operation.get('missing_seconds')) not in (int, float) or
                    operation['missing_seconds'] < 60):
                raise ProtocolError('insufficient_collector_deletion_evidence')
    if value['kind'] == 'baseline' and (value['seq'] != 1 or value['deletes']):
        raise ProtocolError('baseline_cannot_delete')
    return value


def batch_name(manifest):
    return str(manifest['seq']).zfill(12) + '-' + digest(manifest)


def replica_state(cfg):
    home = Path(cfg['replica_state_dir'])
    if (Path(cfg['state_dir']) / 'migration.incomplete.json').exists():
        raise ProtocolError('replica_migration_incomplete')
    if (home / 'commit.json').exists():
        raise ProtocolError('replica_commit_incomplete')
    state = read(home / 'state.json')
    if state['identity'] != [str(Path(cfg['source_root']).resolve()), cfg['scope_id'], cfg['generation']]:
        raise ProtocolError('replica_identity_mismatch')
    if state['worker_state_dir'] != str(Path(cfg['state_dir']).resolve()):
        raise ProtocolError('receiver_and_worker_lock_directory_mismatch')
    if state['root_identity'] != identity(cfg['source_root']) or not state.get('baseline_ready'):
        raise ProtocolError('replica_baseline_not_ready')
    return state


def deletion_receipt(state, name):
    proof = state.get('deletions', {}).get(name)
    if not proof or name in state['files'] or proof.get('scope_id') != state['identity'][1] or \
            proof.get('generation') != state['identity'][2] or not 1 <= proof.get('seq', 0) <= state['last_seq']:
        return None
    return proof
