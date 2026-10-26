"""Commit verified source batches under the existing worker's cycle lock."""
import argparse
import copy
from pathlib import Path
import time

from replica_protocol import (ProtocolError, batch_name, copy_verified, digest, file_hash,
                              identity, inside, locked, read, sync_directory, token,
                              validate_manifest, write)


class Receiver:
    def __init__(self, cfg, fault=None):
        self.cfg, self.fault = cfg, fault or (lambda stage, name: None)
        self.root = Path(cfg['source_root']).resolve()
        self.home = Path(cfg['replica_state_dir']).resolve()
        self.incoming = Path(cfg['incoming_dir']).resolve()
        self.lock = Path(cfg['worker_state_dir']).resolve() / 'worker.lock'
        token(cfg['scope_id']); token(cfg['generation'])
        if any(p.is_relative_to(self.root) or self.root.is_relative_to(p) for p in (self.home, self.incoming)):
            raise ProtocolError('replica_storage_overlaps_sources')
        for folder in (self.root, self.home, self.incoming):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.home / 'state.json'
        self.journal = self.home / 'commit.json'
        with locked(self.lock):
            if not self.file.exists():
                write(self.file, {'identity': [str(self.root), cfg['scope_id'], cfg['generation']],
                     'root_identity': identity(self.root), 'last_seq': 0, 'baseline_ready': False,
                     'worker_state_dir': str(self.lock.parent),
                     'files': {}, 'deletions': {}, 'applied': {}})
            self.load()

    def load(self):
        state = read(self.file)
        if state['identity'] != [str(self.root), self.cfg['scope_id'], self.cfg['generation']] or \
                state['root_identity'] != identity(self.root) or state['worker_state_dir'] != str(self.lock.parent):
            raise ProtocolError('receiver_root_or_binding_changed')
        return state

    def package(self, folder):
        folder = Path(folder)
        if folder.is_symlink() or folder.resolve().parent != self.incoming:
            raise ProtocolError('incoming_package_out_of_scope')
        for name in ('manifest.json', 'READY.json', 'objects'):
            if (folder / name).is_symlink():
                raise ProtocolError('incoming_package_symlink')
        manifest = validate_manifest(read(folder / 'manifest.json'))
        checksum = digest(manifest)
        if read(folder / 'READY.json') != {'manifest_sha256': checksum} or folder.name != batch_name(manifest):
            raise ProtocolError('ready_manifest_mismatch')
        if manifest['scope_id'] != self.cfg['scope_id'] or manifest['generation'] != self.cfg['generation']:
            raise ProtocolError('incoming_binding_mismatch')
        return manifest

    def acknowledgement(self, manifest):
        return {'scope_id': manifest['scope_id'], 'generation': manifest['generation'],
                'seq': manifest['seq'], 'manifest_sha256': digest(manifest), 'status': 'applied'}

    def validate_changes(self, manifest, state, folder):
        if state['last_seq'] == 0 and manifest['kind'] != 'baseline' or \
                state['last_seq'] > 0 and manifest['kind'] != 'delta':
            raise ProtocolError('unexpected_baseline_state')
        if manifest['kind'] == 'baseline':
            expected = {o['path'] for o in manifest['upserts']}
            for path in self.root.rglob('*'):
                if path.is_file() and not any(p.startswith('.') for p in path.relative_to(self.root).parts):
                    if str(path.relative_to(self.root)) not in expected:
                        raise ProtocolError('unlisted_initial_source_file')
        for operation in manifest['upserts']:
            obj = folder / 'objects' / operation['sha256']
            if obj.is_symlink() or not obj.is_file() or obj.stat().st_size != operation['size'] or file_hash(obj) != operation['sha256']:
                raise ProtocolError('incoming_object_invalid')
            target = inside(self.root, operation['path'])
            prior = state['files'].get(operation['path'])
            if target.exists() and file_hash(target) != (prior['sha256'] if prior else operation['sha256']):
                raise ProtocolError('source_replica_modified_outside_receiver')
        for operation in manifest['deletes']:
            prior = state['files'].get(operation['path'])
            if not prior or prior['sha256'] != operation['expected_hash']:
                raise ProtocolError('delete_expected_hash_mismatch')
            target = inside(self.root, operation['path'])
            if target.exists() and file_hash(target) != operation['expected_hash']:
                raise ProtocolError('delete_source_replica_modified')
            original = target if target.exists() else self.incoming / prior['object_batch'] / 'objects' / prior['sha256']
            if not original.is_file() or file_hash(original) != prior['sha256']:
                raise ProtocolError('deleted_source_backup_unavailable')

    def next_state(self, manifest, state):
        after = copy.deepcopy(state)
        for operation in manifest['upserts']:
            after['files'][operation['path']] = {**operation, 'seq': manifest['seq'], 'object_batch': batch_name(manifest)}
            after['deletions'].pop(operation['path'], None)
        for operation in manifest['deletes']:
            after['files'].pop(operation['path'], None)
            after['deletions'][operation['path']] = {**operation, 'scope_id': manifest['scope_id'],
                'generation': manifest['generation'], 'seq': manifest['seq'], 'manifest_sha256': digest(manifest)}
        after.update(last_seq=manifest['seq'], baseline_ready=True, committed_at=time.time())
        after['applied'][str(manifest['seq'])] = digest(manifest)
        return after

    def backup_source(self, name, prior, target, seq):
        backup = inside(self.home / 'source-history' / str(seq), name)
        if backup.exists():
            if file_hash(backup) != prior['sha256']:
                raise ProtocolError('source_history_hash_mismatch')
            return
        original = target if target.exists() else self.incoming / prior['object_batch'] / 'objects' / prior['sha256']
        copy_verified(original, backup, prior['sha256'], prior['size'])

    def apply_journal(self, journal):
        manifest, before, after = journal['manifest'], journal['before'], journal['after']
        folder = self.incoming / batch_name(manifest)
        for operation in manifest['upserts']:
            target = inside(self.root, operation['path'])
            if target.exists() and file_hash(target) == operation['sha256']:
                continue
            prior = before['files'].get(operation['path'])
            if prior:
                self.backup_source(operation['path'], prior, target, manifest['seq'])
            copy_verified(folder / 'objects' / operation['sha256'], target, operation['sha256'], operation['size'])
            self.fault('after_upsert', operation['path'])
        for operation in manifest['deletes']:
            target = inside(self.root, operation['path'])
            prior = before['files'][operation['path']]
            self.backup_source(operation['path'], prior, target, manifest['seq'])
            if target.exists():
                if file_hash(target) != operation['expected_hash']:
                    raise ProtocolError('delete_changed_during_recovery')
                target.unlink()
                sync_directory(target.parent)
            self.fault('after_delete', operation['path'])
        write(self.file, after)
        self.fault('after_state_commit', '')
        self.journal.unlink()
        sync_directory(self.home)
        ack = self.acknowledgement(manifest)
        write(folder / 'ACK.json', ack)
        return ack

    def recover(self):
        with locked(self.lock):
            return self.recover_locked()

    def recover_locked(self):
        self.load()
        if self.journal.exists():
            journal = read(self.journal)
            self.package(self.incoming / batch_name(journal['manifest']))
            return self.apply_journal(journal)
        return None

    def receive(self, folder):
        with locked(self.lock):
            self.recover_locked()
            manifest = self.package(folder)
            state = self.load()
            if manifest['seq'] <= state['last_seq']:
                if state['applied'].get(str(manifest['seq'])) != digest(manifest):
                    raise ProtocolError('same_sequence_different_manifest')
                ack = self.acknowledgement(manifest)
                write(Path(folder) / 'ACK.json', ack)
                return ack
            if manifest['seq'] != state['last_seq'] + 1:
                raise ProtocolError('batch_sequence_gap')
            self.validate_changes(manifest, state, Path(folder))
            journal = {'manifest': manifest, 'before': state, 'after': self.next_state(manifest, state)}
            write(self.journal, journal)
            return self.apply_journal(journal)

    def run_once(self):
        self.recover()
        for folder in sorted(self.incoming.iterdir()):
            if folder.is_dir() and (folder / 'READY.json').is_file() and not (folder / 'ACK.json').exists():
                self.receive(folder)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    cfg = read(args.config)
    receiver = Receiver(cfg)
    while True:
        try:
            receiver.run_once()
            write(receiver.home / 'health.json', {'status': 'running', 'time': time.time(),
                  'last_seq': receiver.load()['last_seq']})
        except (OSError, ProtocolError) as error:
            write(receiver.home / 'health.json', {'status': 'protective_pause', 'time': time.time(),
                  'reason': type(error).__name__ + ':' + str(error)[:120]})
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(cfg.get('interval_seconds', 5))


if __name__ == '__main__':
    main()
