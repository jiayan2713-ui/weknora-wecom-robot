"""Read-only source collection with one durable, unacknowledged file batch."""
import argparse
from collections import Counter
import json
import os
import shutil
from pathlib import Path
import time

from replica_protocol import (ProtocolError, VERSION, batch_name, copy_verified, digest,
                              file_hash, identity, locked, read, relative, signature, token, write)


class Collector:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.root = Path(cfg['source_root']).resolve()
        self.home = Path(cfg['collector_state_dir']).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        token(cfg['scope_id']); token(cfg['generation'])
        if self.home.is_relative_to(self.root):
            raise ProtocolError('collector_state_inside_source')
        self.file = self.home / 'state.json'
        if not self.file.exists():
            write(self.file, {'identity': [str(self.root), cfg['scope_id'], cfg['generation']],
                             'root_identity': identity(self.root), 'seq': 0, 'files': {},
                             'observations': {}, 'missing': {}, 'outbox': None})

    def load(self):
        state = read(self.file)
        if state['identity'] != [str(self.root), self.cfg['scope_id'], self.cfg['generation']] or \
                not self.root.is_dir() or state['root_identity'] != identity(self.root):
            raise ProtocolError('collector_root_or_binding_changed')
        return state

    def paths(self):
        def fail(error):
            raise error
        for directory, folders, files in os.walk(self.root, onerror=fail, followlinks=False):
            folders[:] = [name for name in folders if not name.startswith('.') and
                          not (Path(directory) / name).is_symlink()]
            for name in files:
                yield Path(directory) / name

    def scan(self, state):
        present, placeholders = {}, set()
        for path in self.paths():
            if path.is_symlink() or not path.is_file():
                continue
            raw = str(path.relative_to(self.root))
            if path.suffix == '.icloud':
                logical = path.with_name(path.name.removesuffix('.icloud').removeprefix('.'))
                placeholders.add(relative(str(logical.relative_to(self.root))))
                continue
            if any(part.startswith('.') for part in path.relative_to(self.root).parts):
                continue
            name = relative(raw)
            if name in present:
                raise ProtocolError('normalized_source_name_collision')
            stat = signature(path)
            previous = state['observations'].get(name)
            observation = previous if previous and previous['signature'] == stat else {
                'signature': stat, 'since': self.clock(), 'count': 0}
            observation['count'] += 1
            observation['ready'] = observation['count'] >= 2 and self.clock() - observation['since'] >= self.cfg.get('stable_seconds', 30)
            state['observations'][name] = observation
            present[name] = (path, observation)
        missing = set(state['files']) - set(present) - placeholders
        threshold = max(self.cfg.get('mass_missing_floor', 50),
                        len(state['files']) * self.cfg.get('mass_missing_fraction', .05))
        if len(missing) > threshold:
            raise ProtocolError('mass_collector_missing_protective_pause')
        for name in set(state['missing']) - missing:
            del state['missing'][name]
        for name in missing:
            evidence = state['missing'].setdefault(name, {'since': self.clock(), 'count': 0})
            evidence['count'] += 1
        return present

    def prepare(self):
        with locked(self.home / 'collector.lock'):
            state = self.load()
            if state['outbox']:
                return self.home / state['outbox']
            present = self.scan(state)
            if state['seq'] == 0 and any(not observation['ready'] for _, observation in present.values()):
                write(self.file, state)
                return None
            upserts, deletes = [], []
            for name, (path, observation) in present.items():
                if not observation['ready']:
                    continue
                checksum = observation.get('sha256')
                if not checksum:
                    checksum = file_hash(path)
                    if observation['signature'] != signature(path):
                        raise ProtocolError('source_changed_before_snapshot')
                    observation['sha256'] = checksum
                previous = state['files'].get(name)
                if previous and previous['sha256'] == checksum:
                    continue
                upserts.append({'path': name, 'sha256': checksum, 'size': observation['signature'][2]})
            for name, evidence in state['missing'].items():
                elapsed = self.clock() - evidence['since']
                if evidence['count'] >= max(3, self.cfg.get('delete_missing_scans', 3)) and \
                        elapsed >= max(60, self.cfg.get('delete_grace_seconds', 60)):
                    deletes.append({'path': name, 'expected_hash': state['files'][name]['sha256'],
                                    'missing_scans': evidence['count'], 'missing_seconds': elapsed})
            # Send an unambiguous rename as one commit so the existing worker
            # can retain its source identity and verified parsed artifact.
            new_counts = Counter(o['sha256'] for o in upserts)
            old_counts = Counter(state['files'][name]['sha256'] for name in state['missing'])
            ready_hashes = {o['expected_hash'] for o in deletes}
            upserts = [o for o in upserts if not (new_counts[o['sha256']] == 1 and
                       old_counts[o['sha256']] == 1 and o['sha256'] not in ready_hashes)]
            if not upserts and not deletes and state['seq']:
                write(self.file, state)
                return None
            manifest = {'version': VERSION, 'scope_id': self.cfg['scope_id'],
                        'generation': self.cfg['generation'], 'seq': state['seq'] + 1,
                        'kind': 'baseline' if state['seq'] == 0 else 'delta',
                        'upserts': sorted(upserts, key=lambda x: x['path']),
                        'deletes': sorted(deletes, key=lambda x: x['path'])}
            folder = self.home / 'batches' / batch_name(manifest)
            for operation in upserts:
                path, observation = present[operation['path']]
                destination = folder / 'objects' / operation['sha256']
                if not destination.exists():
                    copy_verified(path, destination, operation['sha256'], operation['size'])
                if signature(path) != observation['signature']:
                    raise ProtocolError('source_changed_during_snapshot')
            write(folder / 'manifest.json', manifest)
            write(folder / 'READY.json', {'manifest_sha256': digest(manifest)})
            state['outbox'] = str(folder.relative_to(self.home))
            write(self.file, state)
            return folder

    def acknowledge(self, ack):
        with locked(self.home / 'collector.lock'):
            state = self.load()
            if not state['outbox']:
                raise ProtocolError('no_unacknowledged_batch')
            folder = self.home / state['outbox']
            manifest = read(folder / 'manifest.json')
            expected = {'scope_id': manifest['scope_id'], 'generation': manifest['generation'],
                        'seq': manifest['seq'], 'manifest_sha256': digest(manifest), 'status': 'applied'}
            if any(ack.get(k) != value for k, value in expected.items()):
                raise ProtocolError('acknowledgement_mismatch')
            for operation in manifest['upserts']:
                state['files'][operation['path']] = {'sha256': operation['sha256'], 'size': operation['size']}
            for operation in manifest['deletes']:
                state['files'].pop(operation['path'], None)
                state['missing'].pop(operation['path'], None)
                state['observations'].pop(operation['path'], None)
            state.update(seq=manifest['seq'], outbox=None, last_ack_at=self.clock())
            write(self.home / 'history' / (str(manifest['seq']) + '.json'), {'manifest': manifest, 'ack': ack})
            write(self.file, state)
            shutil.rmtree(folder)

    def send(self, transport):
        # One publisher across the full prepare -> transmit -> ack interval.
        with locked(self.home / 'publisher.lock'):
            batch = self.prepare()
            if not batch:
                return None
            ack = transport.deliver(batch)
            if ack:
                self.acknowledge(ack)
            return ack


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--transport-config', type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    cfg = read(args.config)
    collector = Collector(cfg)
    while True:
        try:
            if args.transport_config:
                from replica_transport import from_config
                transport = from_config(read(args.transport_config))
                collector.send(transport)
            else:
                collector.prepare()
            write(collector.home / 'health.json', {'time': time.time(), 'status': 'running',
                  'outbox': collector.load()['outbox'], 'last_ack_at': collector.load().get('last_ack_at')})
        except (OSError, ProtocolError) as error:
            write(collector.home / 'health.json', {'time': time.time(), 'status': 'protective_pause',
                  'reason': type(error).__name__ + ':' + str(error)[:120]})
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(cfg.get('interval_seconds', 30))


if __name__ == '__main__':
    main()
